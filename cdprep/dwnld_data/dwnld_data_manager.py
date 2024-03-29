# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Copyright © Climate Data Preprocessing Tool Project Contributors
# https://github.com/cgq-qgc/climate-data-preprocessing-tool
#
# This file is part of Climate Data Preprocessing Tool.
# Licensed under the terms of the GNU General Public License.
# -----------------------------------------------------------------------------

# ---- Standard imports
import csv
from datetime import datetime
import sys
import os
import os.path as osp
from time import gmtime, sleep
from urllib.request import URLError, urlopen

# ---- Third party imports
from appconfigs.base import get_home_dir
import numpy as np
import pandas as pd
from PyQt5.QtCore import pyqtSignal as QSignal
from PyQt5.QtCore import pyqtSlot as QSlot
from PyQt5.QtCore import Qt, QThread, QSize, QObject
from PyQt5.QtWidgets import (
    QWidget, QLabel, QDoubleSpinBox, QComboBox, QFrame, QGridLayout, QSpinBox,
    QPushButton, QApplication, QFileDialog, QGroupBox, QStyle, QMessageBox,
    QProgressBar, QMainWindow)

# ---- Local imports
from cdprep.utils.qthelpers import (
    qbytearray_to_hexstate, hexstate_to_qbytearray)
from cdprep.config.main import CONF
from cdprep.config.icons import get_icon, get_iconsize
from cdprep.widgets.waitingspinner import QWaitingSpinner
from cdprep.dwnld_data.weather_stationlist import WeatherSationView
from cdprep.dwnld_data.weather_station_finder import (
    WeatherStationFinder, PROV_NAME_ABB)


class WaitSpinnerBar(QWidget):

    def __init__(self, parent=None):
        super(WaitSpinnerBar, self).__init__(parent)

        self._layout = QGridLayout(self)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._spinner = QWaitingSpinner(self, centerOnParent=False)

        icon = QWidget().style().standardIcon(QStyle.SP_MessageBoxCritical)
        pixmap = icon.pixmap(QSize(64, 64))
        self._failed_icon = QLabel()
        self._failed_icon.setPixmap(pixmap)
        self._failed_icon.hide()

        self._layout.addWidget(self._spinner, 1, 1)
        self._layout.addWidget(self._failed_icon, 1, 1)
        self._layout.addWidget(self._label, 2, 0, 1, 3)

        self._layout.setRowStretch(0, 100)
        self._layout.setRowStretch(3, 100)
        self._layout.setColumnStretch(0, 100)
        self._layout.setColumnStretch(2, 100)

    def set_label(self, text):
        """Set the text that is displayed next to the spinner."""
        self._label.setText(text)

    def show_warning_icon(self):
        """Stop and hide the spinner and show a critical icon instead."""
        self._spinner.hide()
        self._spinner.stop()
        self._failed_icon.show()

    def show(self):
        """Override Qt show to start waiting spinner."""
        self._spinner.show()
        self._failed_icon.hide()
        super(WaitSpinnerBar, self).show()
        self._spinner.start()

    def hide(self):
        """Override Qt hide to stop waiting spinner."""
        super(WaitSpinnerBar, self).hide()
        self._spinner.stop()


class WeatherStationDownloader(QMainWindow):
    """
    Widget that allows the user to browse and select ECCC climate stations.
    """
    sig_download_process_ended = QSignal()
    ConsoleSignal = QSignal(str)
    staListSignal = QSignal(list)

    PROV_NAME = [x[0].title() for x in PROV_NAME_ABB]
    PROV_ABB = [x[1] for x in PROV_NAME_ABB]

    def __init__(self, parent=None, workdir=None):
        super().__init__(parent)
        self.workdir = workdir or get_home_dir()

        self.stn_finder_worker = WeatherStationFinder()
        self.stn_finder_worker.sig_load_database_finished.connect(
            self.receive_load_database)
        self.stn_finder_thread = QThread()
        self.stn_finder_worker.moveToThread(self.stn_finder_thread)
        self._database_isloading = False

        self.station_table = WeatherSationView()
        self.waitspinnerbar = WaitSpinnerBar()
        self.stn_finder_worker.sig_progress_msg.connect(
            self.waitspinnerbar.set_label)

        self.__initUI__()
        self._restore_window_geometry()

        # Setup the raw data downloader.
        self._dwnld_inprogress = False
        self._dwnld_stations_list = []
        self.dwnld_thread = QThread()
        self.dwnld_worker = RawDataDownloader()
        self.dwnld_worker.moveToThread(self.dwnld_thread)

        self.dwnld_worker.sig_download_finished.connect(
            self.process_station_data)
        self.dwnld_worker.sig_update_pbar.connect(self.progressbar.setValue)

        self.start_load_database()

    def __initUI__(self):
        self.setWindowTitle('Download Weather Data')
        self.setWindowIcon(get_icon('master'))
        self.setWindowFlags(Qt.Window)

        now = datetime.now()

        # Setup the proximity filter group.
        self.lat_spinBox = QDoubleSpinBox()
        self.lat_spinBox.setAlignment(Qt.AlignCenter)
        self.lat_spinBox.setSingleStep(0.1)
        self.lat_spinBox.setDecimals(3)
        self.lat_spinBox.setValue(CONF.get('download_data', 'latitude', 0))
        self.lat_spinBox.setMinimum(0)
        self.lat_spinBox.setMaximum(180)
        self.lat_spinBox.setSuffix(u' °')
        self.lat_spinBox.valueChanged.connect(self.proximity_grpbox_toggled)

        self.lon_spinBox = QDoubleSpinBox()
        self.lon_spinBox.setAlignment(Qt.AlignCenter)
        self.lon_spinBox.setSingleStep(0.1)
        self.lon_spinBox.setDecimals(3)
        self.lon_spinBox.setValue(CONF.get('download_data', 'longitude', 0))
        self.lon_spinBox.setMinimum(0)
        self.lon_spinBox.setMaximum(180)
        self.lon_spinBox.setSuffix(u' °')
        self.lon_spinBox.valueChanged.connect(self.proximity_grpbox_toggled)

        self.radius_SpinBox = QComboBox()
        self.radius_SpinBox.addItems(['25 km', '50 km', '100 km', '200 km'])
        self.radius_SpinBox.setCurrentIndex(CONF.get(
            'download_data', 'radius_index', 0))
        self.radius_SpinBox.currentIndexChanged.connect(
            self.search_filters_changed)

        self.prox_grpbox = QGroupBox("Proximity Filter")
        self.prox_grpbox.setCheckable(True)
        self.prox_grpbox.setChecked(CONF.get(
            'download_data', 'proximity_filter', False))
        self.prox_grpbox.toggled.connect(self.proximity_grpbox_toggled)

        prox_search_grid = QGridLayout(self.prox_grpbox)
        prox_search_grid.addWidget(QLabel('Latitude:'), 0, 0)
        prox_search_grid.addWidget(self.lat_spinBox, 0, 1)
        prox_search_grid.addWidget(QLabel('North'), 0, 2)
        prox_search_grid.addWidget(QLabel('Longitude:'), 1, 0)
        prox_search_grid.addWidget(self.lon_spinBox, 1, 1)
        prox_search_grid.addWidget(QLabel('West'), 1, 2)
        prox_search_grid.addWidget(QLabel('Search Radius:'), 2, 0)
        prox_search_grid.addWidget(self.radius_SpinBox, 2, 1)
        prox_search_grid.setColumnStretch(0, 100)
        prox_search_grid.setRowStretch(3, 100)

        # ---- Province filter
        self.prov_widg = QComboBox()
        self.prov_widg.addItems(['All'] + self.PROV_NAME)
        self.prov_widg.setCurrentIndex(CONF.get(
            'download_data', 'province_index', 0))
        self.prov_widg.currentIndexChanged.connect(self.search_filters_changed)

        prov_grpbox = QGroupBox()
        prov_layout = QGridLayout(prov_grpbox)
        prov_layout.addWidget(QLabel('Province:'), 0, 0)
        prov_layout.addWidget(self.prov_widg, 0, 1)
        prov_layout.setColumnStretch(0, 1)
        prov_layout.setRowStretch(1, 1)

        # ---- Data availability filter

        # Number of years with data
        self.nbrYear = QSpinBox()
        self.nbrYear.setAlignment(Qt.AlignCenter)
        self.nbrYear.setSingleStep(1)
        self.nbrYear.setMinimum(0)
        self.nbrYear.setValue(CONF.get(
            'download_data', 'min_nbr_of_years', 3))
        self.nbrYear.valueChanged.connect(self.search_filters_changed)

        subgrid1 = QGridLayout()
        subgrid1.setContentsMargins(0, 0, 0, 0)
        subgrid1.addWidget(QLabel('for at least'), 0, 0)
        subgrid1.addWidget(self.nbrYear, 0, 1)
        subgrid1.addWidget(QLabel('year(s)'), 0, 2)
        subgrid1.setColumnStretch(3, 100)
        subgrid1.setHorizontalSpacing(5)

        # Year range
        self.minYear = QSpinBox()
        self.minYear.setAlignment(Qt.AlignCenter)
        self.minYear.setSingleStep(1)
        self.minYear.setMinimum(1840)
        self.minYear.setMaximum(now.year)
        self.minYear.setValue(CONF.get(
            'download_data', 'year_range_left_bound', 1840))
        self.minYear.valueChanged.connect(self.minYear_changed)

        label_and = QLabel('and')
        label_and.setAlignment(Qt.AlignCenter)

        self.maxYear = QSpinBox()
        self.maxYear.setAlignment(Qt.AlignCenter)
        self.maxYear.setSingleStep(1)
        self.maxYear.setMinimum(1840)
        self.maxYear.setMaximum(now.year)
        self.maxYear.setValue(CONF.get(
            'download_data', 'year_range_right_bound', now.year))

        self.maxYear.valueChanged.connect(self.maxYear_changed)

        subgrid2 = QGridLayout()
        subgrid2.addWidget(QLabel('between'), 0, 0)
        subgrid2.addWidget(self.minYear, 0, 1)
        subgrid2.addWidget(label_and, 0, 2)
        subgrid2.addWidget(self.maxYear, 0, 3)
        subgrid2.setContentsMargins(0, 0, 0, 0)
        subgrid2.setColumnStretch(4, 100)
        subgrid2.setHorizontalSpacing(5)

        # Subgridgrid assembly
        self.year_widg = QGroupBox("Data Availability filter")
        self.year_widg.setCheckable(True)
        self.year_widg.setChecked(CONF.get(
            'download_data', 'data_availability_filter', False))
        self.year_widg.toggled.connect(self.search_filters_changed)

        grid = QGridLayout(self.year_widg)
        grid.setRowMinimumHeight(0, 10)
        grid.addWidget(QLabel('Search for stations with data available'), 1, 0)
        grid.addLayout(subgrid1, 2, 0)
        grid.addLayout(subgrid2, 3, 0)
        grid.setRowStretch(4, 100)
        grid.setVerticalSpacing(8)

        # Setup the toolbar.
        self.btn_addSta = QPushButton('Add')
        self.btn_addSta.setIcon(get_icon('add2list'))
        self.btn_addSta.setIconSize(get_iconsize('small'))
        self.btn_addSta.setToolTip(
            'Add selected stations to the current list of weather stations.')
        self.btn_addSta.clicked.connect(self.btn_addSta_isClicked)
        self.btn_addSta.hide()

        btn_save = QPushButton('Save')
        btn_save.setToolTip('Save the list of selected stations to a file.')
        btn_save.clicked.connect(self.btn_save_isClicked)
        btn_save.hide()

        self.btn_download = QPushButton('Download')
        self.btn_download.clicked.connect(self.start_download_process)

        btn_close = QPushButton('Close')
        btn_close.clicked.connect(self.close)

        self.btn_fetch = btn_fetch = QPushButton('Refresh')
        btn_fetch.setToolTip(
            "Update the list of climate stations by fetching it from "
            "the ECCC remote location.")
        btn_fetch.clicked.connect(self.btn_fetch_isClicked)

        toolbar_widg = QWidget()
        toolbar_grid = QGridLayout(toolbar_widg)
        toolbar_grid.addWidget(self.btn_addSta, 1, 1)
        toolbar_grid.addWidget(btn_save, 1, 2)
        toolbar_grid.addWidget(btn_fetch, 1, 3)
        toolbar_grid.addWidget(self.btn_download, 1, 4)
        toolbar_grid.addWidget(btn_close, 1, 5)
        toolbar_grid.setColumnStretch(0, 100)
        toolbar_grid.setContentsMargins(0, 10, 0, 0)

        # Setup the left panel.
        self.left_panel = QFrame()
        left_panel_grid = QGridLayout(self.left_panel)
        left_panel_grid.setContentsMargins(0, 0, 0, 0)
        left_panel_grid.addWidget(
            QLabel('Search Criteria'), 0, 0)
        left_panel_grid.addWidget(prov_grpbox, 1, 0)
        left_panel_grid.addWidget(self.prox_grpbox, 2, 0)
        left_panel_grid.addWidget(self.year_widg, 3, 0)
        left_panel_grid.setRowStretch(3, 100)

        # Setup the progress bar.
        self.progressbar = QProgressBar()
        self.progressbar.setValue(0)
        self.progressbar.hide()

        # Setup the central widget.
        main_widget = QWidget()
        main_layout = QGridLayout(main_widget)
        main_layout.addWidget(self.left_panel, 0, 0)
        main_layout.addWidget(self.station_table, 0, 1)
        main_layout.addWidget(self.waitspinnerbar, 0, 1)
        main_layout.addWidget(toolbar_widg, 1, 0, 1, 2)
        main_layout.addWidget(self.progressbar, 2, 0, 1, 2)
        main_layout.setColumnStretch(1, 100)

        self.setCentralWidget(main_widget)

    @property
    def stationlist(self):
        return self.station_table.get_stationlist()

    @property
    def search_by(self):
        return ['proximity', 'province'][self.tab_widg.currentIndex()]

    @property
    def prov(self):
        if self.prov_widg.currentIndex() == 0:
            return self.PROV_NAME
        else:
            return [self.PROV_NAME[self.prov_widg.currentIndex()-1]]

    @property
    def lat(self):
        return self.lat_spinBox.value()

    def set_lat(self, x, silent=True):
        if silent:
            self.lat_spinBox.blockSignals(True)
        self.lat_spinBox.setValue(x)
        self.lat_spinBox.blockSignals(False)
        self.proximity_grpbox_toggled()

    @property
    def lon(self):
        return self.lon_spinBox.value()

    def set_lon(self, x, silent=True):
        if silent:
            self.lon_spinBox.blockSignals(True)
        self.lon_spinBox.setValue(x)
        self.lon_spinBox.blockSignals(False)
        self.proximity_grpbox_toggled()

    @property
    def rad(self):
        return int(self.radius_SpinBox.currentText()[:-3])

    @property
    def prox(self):
        if self.prox_grpbox.isChecked():
            return (self.lat, -self.lon, self.rad)
        else:
            return None

    @property
    def year_min(self):
        return int(self.minYear.value())

    def set_yearmin(self, x, silent=True):
        if silent:
            self.minYear.blockSignals(True)
        self.minYear.setValue(x)
        self.minYear.blockSignals(False)

    @property
    def year_max(self):
        return int(self.maxYear.value())

    def set_yearmax(self, x, silent=True):
        if silent:
            self.maxYear.blockSignals(True)
        self.maxYear.setValue(x)
        self.maxYear.blockSignals(False)

    @property
    def nbr_of_years(self):
        return int(self.nbrYear.value())

    def set_yearnbr(self, x, silent=True):
        if silent:
            self.nbrYear.blockSignals(True)
        self.nbrYear.setValue(x)
        self.nbrYear.blockSignals(False)

    # ---- Load Station Database
    def start_load_database(self, force_fetch=False):
        """Start the process of loading the climate station database."""
        if self._database_isloading is False:
            self._database_isloading = True

            self.station_table.clear()
            self.waitspinnerbar.show()

            # Start the downloading process.
            if force_fetch:
                self.stn_finder_thread.started.connect(
                    self.stn_finder_worker.fetch_database)
            else:
                self.stn_finder_thread.started.connect(
                    self.stn_finder_worker.load_database)
            self.stn_finder_thread.start()

    @QSlot()
    def receive_load_database(self):
        """Handles when loading the database is finished."""
        # Disconnect the thread.
        self.stn_finder_thread.started.disconnect()

        # Quit the thread.
        self.stn_finder_thread.quit()
        waittime = 0
        while self.stn_finder_thread.isRunning():
            sleep(0.1)
            waittime += 0.1
            if waittime > 15:
                print("Unable to quit the thread.")
                break
        # Force an update of the GUI.
        self.proximity_grpbox_toggled()
        if self.stn_finder_worker.data is None:
            self.waitspinnerbar.show_warning_icon()
        else:
            self.waitspinnerbar.hide()
        self._database_isloading = False

    # ---- GUI handlers
    def minYear_changed(self):
        min_yr = min_yr = max(self.minYear.value(), 1840)

        now = datetime.now()
        max_yr = now.year

        self.maxYear.setRange(min_yr, max_yr)
        self.search_filters_changed()

    def maxYear_changed(self):
        min_yr = 1840

        now = datetime.now()
        max_yr = min(self.maxYear.value(), now.year)

        self.minYear.setRange(min_yr, max_yr)
        self.search_filters_changed()

    # ---- Toolbar Buttons Handlers
    def btn_save_isClicked(self):
        ddir = os.path.join(os.getcwd(), 'weather_station_list.csv')
        filename, ftype = QFileDialog().getSaveFileName(
            self, 'Save normals', ddir, '*.csv;;*.xlsx;;*.xls')
        self.station_table.save_stationlist(filename)

    def btn_addSta_isClicked(self):
        rows = self.station_table.get_checked_rows()
        if len(rows) > 0:
            staList = self.station_table.get_content4rows(rows)
            self.staListSignal.emit(staList)
            print('Selected stations sent to list')
        else:
            print('No station currently selected')

    def btn_fetch_isClicked(self):
        """Handles when the button fetch is clicked."""
        self.start_load_database(force_fetch=True)

    # ---- Search Filters Handlers
    def proximity_grpbox_toggled(self):
        """
        Set the values for the reference geo coordinates that are used in the
        WeatherSationView to calculate the proximity values and forces a
        refresh of the content of the table.
        """
        if self.prox_grpbox.isChecked():
            self.station_table.set_geocoord((self.lat, -self.lon))
        else:
            self.station_table.set_geocoord(None)
        self.search_filters_changed()

    def search_filters_changed(self):
        """
        Search for weather stations with the current filter values and forces
        an update of the station table content.
        """
        if self.stn_finder_worker.data is not None:
            stnlist = self.stn_finder_worker.get_stationlist(
                prov=self.prov,
                prox=self.prox,
                yrange=((self.year_min, self.year_max, self.nbr_of_years) if
                        self.year_widg.isChecked() else
                        None)
                )
            self.station_table.populate_table(stnlist)

    # ---- Download weather data
    def start_download_process(self):
        """Start the downloading process of raw weather data files."""
        if self._dwnld_inprogress is True:
            self.stop_download_process()
            return

        # Grab the info of the weather stations that are selected.
        rows = self.station_table.get_checked_rows()
        self._dwnld_stations_list = self.station_table.get_content4rows(rows)
        if len(self._dwnld_stations_list) == 0:
            QMessageBox.warning(
                self, 'Warning',
                "No weather station currently selected.",
                QMessageBox.Ok)
            return

        # Update the UI.
        self.progressbar.show()
        self.btn_download.setText("Cancel")
        self.left_panel.setEnabled(False)
        self.station_table.setEnabled(False)
        self.btn_fetch.setEnabled(False)

        # Set thread working directory.
        self.dwnld_worker.dirname = self.workdir

        # Start downloading data.
        self._dwnld_inprogress = True
        self.download_next_station()

    def stop_download_process(self):
        """Stop the downloading process."""
        print('Stopping the download process...')
        self.dwnld_worker.stop_download()
        self._dwnld_stations_list = []
        self.btn_download.setEnabled(False)

        self.wait_for_thread_to_quit()
        self.btn_download.setEnabled(True)
        self.btn_download.setText("Download")
        self.left_panel.setEnabled(True)
        self.station_table.setEnabled(True)
        self.btn_fetch.setEnabled(True)

        self._dwnld_inprogress = False
        self.sig_download_process_ended.emit()
        print('Download process stopped.')

    def download_next_station(self):
        self.wait_for_thread_to_quit()
        try:
            dwnld_station = self._dwnld_stations_list.pop(0)
        except IndexError:
            # There is no more data to download.
            print('Raw weather data downloaded for all selected stations.')
            self.btn_download.setText("Download")
            self.progressbar.hide()
            self.left_panel.setEnabled(True)
            self.station_table.setEnabled(True)
            self.btn_fetch.setEnabled(True)
            self._dwnld_inprogress = False
            self.sig_download_process_ended.emit()
            return

        # Set worker attributes.
        self.dwnld_worker.StaName = dwnld_station[0]
        self.dwnld_worker.stationID = dwnld_station[1]
        self.dwnld_worker.yr_start = dwnld_station[2]
        self.dwnld_worker.yr_end = dwnld_station[3]
        self.dwnld_worker.climateID = dwnld_station[5]

        # Highlight the row of the next station to download data from.
        self.station_table.selectRow(
            self.station_table.get_row_from_climateid(dwnld_station[5]))

        # Start the downloading process.
        try:
            self.dwnld_thread.started.disconnect(
                self.dwnld_worker.download_data)
        except TypeError:
            # The method self.dwnld_worker.download_data is not connected.
            pass
        finally:
            self.dwnld_thread.started.connect(self.dwnld_worker.download_data)
            self.dwnld_thread.start()

    def wait_for_thread_to_quit(self):
        self.dwnld_thread.quit()
        waittime = 0
        while self.dwnld_thread.isRunning():
            print('Waiting for the downloading thread to close')
            sleep(0.1)
            waittime += 0.1
            if waittime > 15:
                print("Unable to close the weather data dowloader thread.")
                return

    def process_station_data(self, climateid, file_list=None):
        """
        Read, concatenate, and save to csv the raw weather data that were
        just downloaded for the station corresponding to the specified
        climate ID.
        """
        if file_list:
            station_metadata = self.station_table.get_content4rows(
                [self.station_table.get_row_from_climateid(climateid)])[0]
            station_data = read_raw_datafiles(file_list)
            print('Formating and concatenating raw data for station {}.'
                  .format(station_metadata[0]))

            # Define the concatenated filename.
            station_name = (
                station_metadata[0].replace('\\', '_').replace('/', '_'))
            min_year = min(station_data.index).year
            max_year = max(station_data.index).year
            filename = osp.join("%s (%s)_%s-%s.csv" % (
                station_name, climateid, min_year, max_year))

            # Save the concatenated data to csv.
            data_headers = ['Year', 'Month', 'Day', 'Max Temp (°C)',
                            'Min Temp (°C)', 'Mean Temp (°C)',
                            'Total Precip (mm)']
            fcontent = [
                ['Station Name', station_metadata[0]],
                ['Province', station_metadata[4]],
                ['Latitude (dd)', station_metadata[6]],
                ['Longitude (dd)', station_metadata[7]],
                ['Elevation (m)', station_metadata[8]],
                ['Climate Identifier', station_metadata[5]],
                [],
                data_headers]
            fcontent = fcontent + station_data[data_headers].values.tolist()

            # Save the data to csv.
            filepath = osp.join(self.dwnld_worker.dirname, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                writer = csv.writer(f, delimiter=',', lineterminator='\n')
                writer.writerows(fcontent)
        self.download_next_station()

    # ---- Main window settings
    def _restore_window_geometry(self):
        """
        Restore the geometry of this mainwindow from the value saved
        in the config.
        """
        hexstate = CONF.get('download_data', 'window/geometry', None)
        if hexstate:
            hexstate = hexstate_to_qbytearray(hexstate)
            self.restoreGeometry(hexstate)
        else:
            self.resize(1000, 450)

    def _save_window_geometry(self):
        """
        Save the geometry of this mainwindow to the config.
        """
        hexstate = qbytearray_to_hexstate(self.saveGeometry())
        CONF.set('download_data', 'window/geometry', hexstate)

    # ---- Qt overrides
    def closeEvent(self, event):
        self._save_window_geometry()

        # Proximity Filter Options.
        CONF.set('download_data', 'proximity_filter',
                 self.prox_grpbox.isChecked())
        CONF.set('download_data', 'latitude', self.lat)
        CONF.set('download_data', 'longitude', self.lon)
        CONF.set('download_data', 'radius_index',
                 self.radius_SpinBox.currentIndex())
        CONF.set('download_data', 'province_index',
                 self.prov_widg.currentIndex())

        # Data Availability Filter Options.
        CONF.set('download_data', 'data_availability_filter',
                 self.year_widg.isChecked())
        CONF.set('download_data', 'min_nbr_of_years',
                 self.nbrYear.value())
        CONF.set('download_data', 'year_range_left_bound',
                 self.minYear.value())
        CONF.set('download_data', 'year_range_right_bound',
                 self.maxYear.value())
        event.accept()


class RawDataDownloader(QObject):
    """
    This class is used to download the raw data files from
    www.climate.weather.gc.ca and saves them automatically in
    <Project_directory>/Meteo/Raw/<station_name (Climate ID)>.

    ERRFLAG = Flag for the download of files - np.arrays
                  0 -> File downloaded successfully
                  1 -> Problem downloading the file
                  3 -> File NOT downloaded because it already exists
    """

    sig_download_finished = QSignal(str, list)
    sig_update_pbar = QSignal(int)
    ConsoleSignal = QSignal(str)

    def __init__(self):
        super(RawDataDownloader, self).__init__(parent=None)

        self.__stop_dwnld = False

        self.ERRFLAG = []

        # These values need to be pushed from the parent.

        self.dirname = None  # Directory where the downloaded files are saved
        self.stationID = []
        # Unique identifier for the station used for downloading the
        # data from the server
        self.climateID = None  # Unique identifier for the station
        self.yr_start = None
        self.yr_end = None
        self.StaName = None  # Common name given to the station (not unique)

    def stop_download(self):
        self.__stop_dwnld = True

    def download_data(self):
        """
        Download raw data files on a yearly basis from yr_start to yr_end.
        """

        staID = self.stationID
        yr_start = int(self.yr_start)
        yr_end = int(self.yr_end)
        StaName = self.StaName
        climateID = self.climateID

        self.ERRFLAG = np.ones(yr_end - yr_start + 1)

        print("Downloading data for station %s" % StaName)
        self.sig_update_pbar.emit(0)

        StaName = StaName.replace('\\', '_')
        StaName = StaName.replace('/', '_')
        dirname = osp.join(
            self.dirname, 'RAW', '%s (%s)' % (StaName, climateID))
        if not osp.exists(dirname):
            os.makedirs(dirname)

        # Data are downloaded on a yearly basis from yStart to yEnd
        downloaded_raw_datafiles = []
        for i, year in enumerate(range(yr_start, yr_end+1)):
            if self.__stop_dwnld:
                # Stop the downloading process.
                self.__stop_dwnld = False
                print("Downloading process for station {} stopped.".format(
                    StaName))
                return

            # Define file and URL paths.
            fname = osp.join(
                dirname, "eng-daily-0101%s-1231%s.csv" % (year, year))
            url = ('http://climate.weather.gc.ca/climate_data/' +
                   'bulk_data_e.html?format=csv&stationID=' + str(staID) +
                   '&Year=' + str(year) + '&Month=1&Day=1&timeframe=2' +
                   '&submit=Download+Data')

            # Download data for that year.
            if osp.exists(fname):
                # If the file was downloaded in the same year that of the data
                # record, data will be downloaded again in case the data series
                # was not complete.

                # Get year of file last modification
                myear = osp.getmtime(fname)
                myear = gmtime(myear)[0]
                if myear == year:
                    self.ERRFLAG[i] = self.download_file(url, fname)
                else:
                    self.ERRFLAG[i] = 3
                    print('    %s: Raw data file already exists for year %d.' %
                          (StaName, year))
            else:
                self.ERRFLAG[i] = self.download_file(url, fname)
                print('    %s: Downloading raw data file for year %d.' %
                      (StaName, year))

            # Update UI :

            progress = (year - yr_start+1) / (yr_end+1 - yr_start) * 100
            self.sig_update_pbar.emit(int(progress))

            if self.ERRFLAG[i] == 1:
                self.ConsoleSignal.emit(
                    '''<font color=red>There was a problem downloading the
                         data of station %s for year %d.
                       </font>''' % (StaName, year))
            elif self.ERRFLAG[i] == 0:
                self.ConsoleSignal.emit(
                    '''<font color=black>Weather data for station %s
                         downloaded successfully for year %d.
                       </font>''' % (StaName, year))
                downloaded_raw_datafiles.append(fname)
            elif self.ERRFLAG[i] == 3:
                sleep(0.1)
                self.ConsoleSignal.emit(
                    '''<font color=green>A weather data file already existed
                         for station %s for year %d. Downloading is skipped.
                       </font>''' % (StaName, year))
                downloaded_raw_datafiles.append(fname)

        if self.__stop_dwnld is False:
            print("All raw data downloaded sucessfully for station {}.".format(
                StaName))
            self.sig_update_pbar.emit(0)
            self.sig_download_finished.emit(
                self.climateID, downloaded_raw_datafiles)
            return downloaded_raw_datafiles
        else:
            # The downloading process was stopped by the user.
            self.__stop_dwnld = False
            print("Downloading process for station {} stopped.".format(
                StaName))

    def download_file(self, url, fname):
        """Download the single csv weather data file at the specified url."""
        try:
            ERRFLAG = 0
            f = urlopen(url)

            # Write downloaded content to local file.
            with open(fname, 'wb') as local_file:
                local_file.write(f.read())
        except URLError as e:
            ERRFLAG = 1
            if hasattr(e, 'reason'):
                print('Failed to reach a server.')
                print('Reason: ', e.reason)
            elif hasattr(e, 'code'):
                print('The server couldn\'t fulfill the request.')
                print('Error code: ', e.code)
        return ERRFLAG


def read_raw_datafiles(filenames):
    """
    Read, format and concatenate the weather data from a list of csv files
    downloaded from the climate.weather.gc.ca website.
    """
    dataset = None
    for filename in filenames:
        if dataset is None:
            dataset = read_raw_datafile(filename)
        else:
            dataset = dataset.append(read_raw_datafile(filename))
    return dataset


def read_raw_datafile(filename):
    """
    Read and format the weather data from one csv file downloaded from the
    climate.weather.gc.ca website.
    """
    dataset = pd.read_csv(filename, dtype='str')
    valid_columns = [
        'Date/Time', 'Year', 'Month', 'Day', 'Max Temp (°C)', 'Min Temp (°C)',
        'Mean Temp (°C)', 'Total Precip (mm)']
    dataset['Date/Time'] = pd.to_datetime(
        dataset['Date/Time'], format="%Y-%m-%d")
    dataset = (
        dataset
        .drop(labels=[c for c in dataset.columns if c not in valid_columns],
              axis=1)
        .set_index('Date/Time', drop=True)
        )
    return dataset


if __name__ == '__main__':
    app = QApplication(sys.argv)

    stn_browser = WeatherStationDownloader()
    stn_browser.show()

    sys.exit(app.exec_())
