"""
app.py

The movie library window (PySide6 / Qt). Layout, following your sketch:

    +------------------------------------------------------+
    | Search box                     Sort by [..]  Expand  |
    +----------------------+-------------------------------+
    | Filters (collapsible)| Movie / file tree             |
    |   Confidence         |   Movie                       |
    |   Drives             |     D:\\path\\movie.mkv         |
    |   Network shares     |   ...                         |
    |   Plex servers       |                               |
    |   Folders to exclude |                               |
    | Info (totals)        |                               |
    +----------------------+-------------------------------+
    | Status bar                                           |
    +------------------------------------------------------+

All filtering rules live in queries.py; this file only builds widgets,
reads their state, and shows the results. The database is opened
read-only, so the UI can never damage your data.

Run with:  python app.py
Requires:  python -m pip install PySide6
"""

import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import quote_plus

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QObject,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTabBar,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import queries
import scanner
from settings import load_settings, save_settings

DB_PATH = Path(__file__).parent / "movies.db"

# Delay (milliseconds) after the last change before the list is refreshed,
# so typing quickly in the search box doesn't re-run the query per letter.
REFRESH_DELAY_MS = 200

# Delay (milliseconds) after the last change before settings are written to
# disk. Saving a moment after changes (not only on exit) means a crash or
# power cut can't lose your settings.
SAVE_DELAY_MS = 1000

CONFIDENCE_ICONS = {"strong": "✅", "basic": "🟡", "low": "⚠️"}

# Zoom: the whole UI font is scaled between these limits, in these steps.
ZOOM_STEP = 0.1
ZOOM_MIN = 0.6
ZOOM_MAX = 3.0

# "Rescan Drives" asks "are you sure?" only when the last scan took longer
# than this many seconds (or has never been timed). A quick scan makes a
# misclick harmless, so it just runs.
CONFIRM_RESCAN_IF_SLOWER_THAN = 15

# Size of the toolbar icons at 100% zoom, in pixels.
TOOLBAR_ICON_SIZE = 28

# The search box text is this many times bigger than the normal UI text.
SEARCH_FONT_SCALE = 1.5


class CollapsibleSection(QWidget):
    """A titled section that expands/collapses and holds checkboxes.

    Emits `changed` whenever any checkbox in it changes, so the main window
    knows to refresh the results.
    """

    changed = Signal()

    def __init__(self, title, expanded=True, select_buttons=True):
        super().__init__()

        self.base_title = title

        # Header row: arrow + title on the left, All / None on the right.
        self.toggle_button = QToolButton()
        self.toggle_button.setText(title)
        self.expanded = expanded
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        # Flat and bold. Deliberately NOT done with a stylesheet: any
        # stylesheet stops the widget following zoom changes, whereas
        # setBold() changes only the weight and still inherits the size.
        # (The button isn't "checkable" either, since a checked button is
        # drawn as pressed-in; we track expanded/collapsed ourselves.)
        self.toggle_button.setAutoRaise(True)
        header_font = self.toggle_button.font()
        header_font.setBold(True)
        self.toggle_button.setFont(header_font)
        self.toggle_button.clicked.connect(self._on_clicked)

        header = QHBoxLayout()
        header.addWidget(self.toggle_button)
        header.addStretch()
        if select_buttons:
            for label, state in (("All", True), ("None", False)):
                button = QToolButton()
                button.setText(label)
                button.setAutoRaise(True)
                button.clicked.connect(lambda _=False, s=state: self.set_all(s))
                header.addWidget(button)

        # Body: the checkboxes (or any other widgets) live here.
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(18, 0, 0, 4)
        self.content.setVisible(expanded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(self.content)

        self.checkboxes = []   # list of (QCheckBox, data) pairs
        self.box_by_key = {}   # stable name -> QCheckBox, used for saving
        self._update_arrow()

    def _on_clicked(self):
        self.expanded = not self.expanded
        self.content.setVisible(self.expanded)
        self._update_arrow()

    def _update_arrow(self):
        arrow = Qt.DownArrow if self.expanded else Qt.RightArrow
        self.toggle_button.setArrowType(arrow)

    def add_checkbox(self, text, checked, data=None, key=None):
        """Add a checkbox carrying `data` (e.g. a place id) with it.

        `key` is a stable name used for saving (a place's NAME, never its
        id, because ids change when the database is rebuilt).
        """
        box = QCheckBox(text)
        box.setChecked(checked)
        box.toggled.connect(self._on_box_toggled)
        self.content_layout.addWidget(box)
        self.checkboxes.append((box, data))
        self.box_by_key[key if key is not None else text] = box
        self._update_title()
        return box

    def _on_box_toggled(self, _checked):
        self._update_title()
        self.changed.emit()

    def _update_title(self):
        """Show "Title (ticked/total)" so a collapsed section still tells
        you how it's filtering."""
        total = len(self.checkboxes)
        if total:
            ticked = len(self.checked_data())
            self.toggle_button.setText(f"{self.base_title} ({ticked}/{total})")

    def add_widget(self, widget):
        """Add any non-checkbox widget to the body."""
        self.content_layout.addWidget(widget)

    def checked_data(self):
        """The `data` of every ticked checkbox."""
        return [data for box, data in self.checkboxes if box.isChecked()]

    def get_state(self):
        """Expanded/collapsed plus every checkbox, as a saveable dict."""
        return {
            "expanded": self.expanded,
            "checked": {key: box.isChecked() for key, box in self.box_by_key.items()},
        }

    def apply_state(self, state):
        """Restore a dict from get_state(). Boxes missing from the saved
        data (a brand-new drive, say) keep their default."""
        self.expanded = bool(state.get("expanded", self.expanded))
        self.content.setVisible(self.expanded)
        self._update_arrow()
        for key, value in state.get("checked", {}).items():
            box = self.box_by_key.get(key)
            if box is not None:
                box.blockSignals(True)   # don't trigger a refresh per box
                box.setChecked(bool(value))
                box.blockSignals(False)
        self._update_title()

    def set_all(self, state):
        """Tick or untick every checkbox, refreshing only once at the end."""
        for box, _ in self.checkboxes:
            box.blockSignals(True)
            box.setChecked(state)
            box.blockSignals(False)
        self._update_title()
        self.changed.emit()


class TaskWorker(QObject):
    """Runs one long task on a background thread so the window stays usable.

    The task is a function(progress, note, cancel_event) -> result:
      progress(done, total, message)  report how far along it is
      note(text)                      pass back a message for the user
      cancel_event                    a threading.Event; set when Cancel is clicked
    The signals below carry its updates back to the window safely.
    """

    progress = Signal(int, int, str)
    note = Signal(str)
    finished = Signal(object, object)  # (result, error): one of them is None

    def __init__(self, task):
        super().__init__()
        self.task = task
        self.cancel_event = threading.Event()

    @Slot()
    def run(self):
        try:
            result = self.task(self.progress.emit, self.note.emit, self.cancel_event)
            self.finished.emit(result, None)
        except Exception as error:  # reported in a message box, not a crash
            self.finished.emit(None, error)


class MainWindow(QMainWindow):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.setWindowTitle("Movie Library")

        # Refresh is debounced through this timer (see REFRESH_DELAY_MS).
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(REFRESH_DELAY_MS)
        self.refresh_timer.timeout.connect(self.refresh)

        # 100% zoom = whatever font the system gave the application.
        self.base_font = QApplication.font()
        if self.base_font.pointSizeF() <= 0:
            self.base_font.setPointSizeF(9.0)
        self.zoom_factor = 1.0

        # Settings are saved shortly after the last change (see SAVE_DELAY_MS).
        # `_ready` stays False during startup so that restoring the saved
        # state doesn't immediately trigger a pointless save.
        self._ready = False
        self._status_text = ""        # the normal status bar message
        self._thread = None           # background task, if one is running
        self._worker = None
        self._task_notes = []
        self.last_scan_seconds = None  # how long the last rescan took
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(SAVE_DELAY_MS)
        self.save_timer.timeout.connect(self.save_now)

        self._build_ui()
        self._build_toolbar()
        self._build_view_menu()
        # An application-wide event filter lets Ctrl + mouse wheel zoom
        # no matter which widget the mouse is over (see eventFilter below).
        QApplication.instance().installEventFilter(self)

        self._apply_settings(load_settings())
        self.set_zoom(self.zoom_factor)  # applies fonts at the restored zoom
        self.refresh()                   # first fill of the results tree
        self._ready = True

    # ------------------------------------------------------------------
    # Building the window
    # ------------------------------------------------------------------
    def _build_ui(self):
        # --- Top row: search, sort, expand/collapse ---
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search movies...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self.schedule_refresh)
        # Wide but capped, so it stays tidy on a fullscreen window. The
        # font size and height are set in set_zoom() so they follow zoom.
        self.search_box.setMinimumWidth(450)
        self.search_box.setMaximumWidth(950)
        self.search_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.sort_box = QComboBox()
        self.sort_box.addItems(queries.SORT_OPTIONS.keys())
        self.sort_box.currentIndexChanged.connect(self.schedule_refresh)

        expand_button = QPushButton("Expand all")
        collapse_button = QPushButton("Collapse all")

        # Equal stretches on both sides keep the search box centered.
        top_row = QHBoxLayout()
        top_row.addStretch(1)
        top_row.addWidget(self.search_box, stretch=4)
        top_row.addStretch(1)

        # Sort and expand/collapse sit just above the results list.
        # Scope tabs (All / Watched / Watchlist) on the left of the same row.
        self.scope_keys = list(queries.SCOPES)
        self.scope_bar = QTabBar()
        self.scope_bar.setDrawBase(False)
        self.scope_bar.setExpanding(False)
        for key in self.scope_keys:
            self.scope_bar.addTab(queries.SCOPES[key])
        self.scope_bar.currentChanged.connect(self.schedule_refresh)

        controls_row = QHBoxLayout()
        controls_row.addWidget(self.scope_bar)
        controls_row.addStretch()
        controls_row.addWidget(QLabel("Sort by:"))
        controls_row.addWidget(self.sort_box)
        controls_row.addWidget(expand_button)
        controls_row.addWidget(collapse_button)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(controls_row)

        # --- Results tree (right side) ---
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Movie / File", "Year", "Match", "Size", "Place", "Logged"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        # The first column takes the leftover space; the others fit their
        # contents, so nothing is cut off at any zoom level.
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        expand_button.clicked.connect(self.tree.expandAll)
        collapse_button.clicked.connect(self.tree.collapseAll)

        # --- Sidebar (left side): filters on top, info panel below ---
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)

        filter_area = QWidget()
        self.filter_layout = QVBoxLayout(filter_area)
        self._build_filter_sections()
        self.filter_layout.addStretch()

        self.filter_scroll = QScrollArea()
        self.filter_scroll.setWidgetResizable(True)
        self.filter_scroll.setWidget(filter_area)
        self.filter_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sidebar_layout.addWidget(self.filter_scroll, stretch=1)

        info_box = QGroupBox("Info")
        info_layout = QVBoxLayout(info_box)
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setTextFormat(Qt.RichText)
        info_layout.addWidget(self.info_label)
        sidebar_layout.addWidget(info_box)

        # --- Put it all together ---
        self.splitter = splitter = QSplitter(Qt.Horizontal)
        right_layout.addWidget(self.tree)
        splitter.addWidget(sidebar)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 900])
        splitter.splitterMoved.connect(self.schedule_save)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.addLayout(top_row)
        central_layout.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)

    def _build_filter_sections(self):
        """Create the collapsible filter sections from the database."""
        # Confidence (fixed choices). Defaults match your preference:
        # trusted matches on, noisy low matches and "not found" off.
        self.confidence_section = CollapsibleSection("Confidence", expanded=True)
        self.confidence_section.add_checkbox("✅ Strong", True, "strong", key="strong")
        self.confidence_section.add_checkbox("🟡 Basic", True, "basic", key="basic")
        self.confidence_section.add_checkbox("⚠️ Low", False, "low", key="low")
        self.confidence_section.add_checkbox("❌ Not found", False, "not_found", key="not_found")
        self.confidence_section.changed.connect(self.schedule_refresh)
        self.confidence_section.toggle_button.clicked.connect(self.schedule_save)
        self.filter_layout.addWidget(self.confidence_section)

        # Places come from the data: one checkbox per drive, share or server.
        places = queries.get_places(self.conn)
        section_specs = [
            ("local", "Drives", True, True),            # (kind, title, expanded, ticked)
            ("network", "Network shares", True, True),
            ("remote", "Plex servers", False, False),   # off by default
        ]
        self.place_sections = {}
        for kind, title, expanded, ticked in section_specs:
            kind_places = [p for p in places if p.kind == kind]
            if not kind_places:
                continue  # no such places in the database yet
            section = CollapsibleSection(title, expanded=expanded)
            for place in kind_places:
                section.add_checkbox(place.name, ticked, place.id, key=place.name)
            section.changed.connect(self.schedule_refresh)
            section.toggle_button.clicked.connect(self.schedule_save)
            self.filter_layout.addWidget(section)
            self.place_sections[kind] = section

        # Folders to exclude: a list plus Add / Remove buttons.
        self.exclude_section = CollapsibleSection(
            "Folders to exclude", expanded=False, select_buttons=False
        )
        self.exclude_list = QListWidget()
        self.exclude_list.setMaximumHeight(110)
        self.exclude_list.setSelectionMode(QListWidget.ExtendedSelection)
        add_button = QPushButton("Add folder...")
        remove_button = QPushButton("Remove selected")
        add_button.clicked.connect(self._add_excluded_folder)
        remove_button.clicked.connect(self._remove_excluded_folders)
        button_row = QHBoxLayout()
        button_row.addWidget(add_button)
        button_row.addWidget(remove_button)
        self.exclude_section.toggle_button.clicked.connect(self.schedule_save)
        self.exclude_section.add_widget(self.exclude_list)
        self.exclude_section.content_layout.addLayout(button_row)
        self.filter_layout.addWidget(self.exclude_section)

    # ------------------------------------------------------------------
    # Reading the controls and refreshing the results
    # ------------------------------------------------------------------
    def schedule_refresh(self, *_):
        """(Re)start the short timer; the refresh happens when it fires."""
        self.refresh_timer.start()
        self.schedule_save()

    # ------------------------------------------------------------------
    # Saved settings
    # ------------------------------------------------------------------
    def schedule_save(self, *_):
        """(Re)start the save timer; the file is written when it fires."""
        if self._ready:
            self.save_timer.start()

    def _collect_settings(self):
        """Gather everything worth remembering from the widgets."""
        sections = {"confidence": self.confidence_section.get_state(),
                    "exclude": self.exclude_section.get_state()}
        for kind, section in self.place_sections.items():
            sections[kind] = section.get_state()
        return {
            "zoom": self.zoom_factor,
            # Window position/size/maximized, packed as text for the JSON file.
            "geometry": bytes(self.saveGeometry().toBase64()).decode("ascii"),
            "splitter": self.splitter.sizes(),
            "sort": self.sort_box.currentText(),
            "scope": self.scope_key(),
            "last_scan_seconds": self.last_scan_seconds,
            "excluded_folders": self._excluded_folders(),
            "sections": sections,
        }

    def save_now(self):
        save_settings(self._collect_settings())

    def _apply_settings(self, saved):
        """Put saved values back into the widgets (anything missing or
        invalid is simply skipped, leaving the default in place)."""
        zoom = saved.get("zoom")
        if isinstance(zoom, (int, float)):
            self.zoom_factor = max(ZOOM_MIN, min(ZOOM_MAX, float(zoom)))

        geometry = saved.get("geometry")
        if isinstance(geometry, str):
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))

        sizes = saved.get("splitter")
        if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(n, int) for n in sizes):
            self.splitter.setSizes(sizes)

        sort_name = saved.get("sort")
        if sort_name in queries.SORT_OPTIONS:
            self.sort_box.blockSignals(True)
            self.sort_box.setCurrentText(sort_name)
            self.sort_box.blockSignals(False)

        scope = saved.get("scope")
        if scope in self.scope_keys:
            self.scope_bar.blockSignals(True)
            self.scope_bar.setCurrentIndex(self.scope_keys.index(scope))
            self.scope_bar.blockSignals(False)

        seconds = saved.get("last_scan_seconds")
        if isinstance(seconds, (int, float)):
            self.last_scan_seconds = float(seconds)

        self._apply_filter_state(saved)

    def _apply_filter_state(self, saved):
        """Restore the sidebar's checkboxes, section states and exclusions."""
        for folder in saved.get("excluded_folders", []):
            if isinstance(folder, str):
                self.exclude_list.addItem(folder)

        sections = saved.get("sections", {})
        all_sections = {"confidence": self.confidence_section,
                        "exclude": self.exclude_section, **self.place_sections}
        for name, section in all_sections.items():
            if isinstance(sections.get(name), dict):
                section.apply_state(sections[name])

    def _rebuild_filter_sections(self):
        """Rebuild the sidebar from the database (after a scan may have
        added new drives or servers), keeping every choice you'd made."""
        saved = self._collect_settings()
        while self.filter_layout.count():
            item = self.filter_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._build_filter_sections()
        self.filter_layout.addStretch()
        self._apply_filter_state(saved)

    def closeEvent(self, event):
        """Stop any running task, then save one last time."""
        if self._thread is not None:
            self._worker.cancel_event.set()
            self._thread.quit()
            self._thread.wait(15000)  # give it a moment to wind down
        self.save_now()
        super().closeEvent(event)

    def _checked_place_ids(self, kinds):
        ids = []
        for kind in kinds:
            if kind in self.place_sections:
                ids += self.place_sections[kind].checked_data()
        return ids

    def _excluded_folders(self):
        return [self.exclude_list.item(i).text() for i in range(self.exclude_list.count())]

    def refresh(self):
        """Run the query for the current filters and redraw everything."""
        confidence_choices = self.confidence_section.checked_data()
        results = queries.fetch_results(
            self.conn,
            search=self.search_box.text(),
            confidences=[c for c in confidence_choices if c != "not_found"],
            local_place_ids=self._checked_place_ids(["local", "network"]),
            remote_place_ids=self._checked_place_ids(["remote"]),
            include_not_found="not_found" in confidence_choices,
            excluded_folders=self._excluded_folders(),
            sort_name=self.sort_box.currentText(),
            scope=self.scope_key(),
        )
        self._fill_tree(results)
        self._update_info(results)

    def _fill_tree(self, results):
        # Turning off repainting while we add thousands of rows is much faster.
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        bold = QFont(self.tree.font())
        bold.setBold(True)
        sort_name = self.sort_box.currentText()
        # In the Watchlist scope the date column means "added to watchlist".
        self.tree.headerItem().setText(5, "Added" if self.scope_key() == "watchlist" else "Logged")

        for movie in results:
            logged = queries.displayed_date(movie, sort_name) or ""
            top = QTreeWidgetItem([movie.title, str(movie.year or ""), "", "", "", logged])
            top.setFont(0, bold)
            # Stash (title, year, Letterboxd link) for the right-click menu.
            top.setData(0, Qt.UserRole + 1, (movie.title, movie.year, movie.letterboxd_url))
            # Hovering a movie lists every dated log (rewatches included).
            tooltip = queries.describe_logs(movie)
            if tooltip:
                top.setToolTip(0, tooltip)
                top.setToolTip(5, tooltip)
            self.tree.addTopLevelItem(top)

            if not movie.files and not movie.servers:
                top.setText(2, "❌ Not found")
            elif not movie.files:
                top.setText(2, "☁ Plex only")

            for f in movie.files:
                child = QTreeWidgetItem(top, [
                    f.path, "",
                    f"{CONFIDENCE_ICONS[f.confidence]} {f.confidence.capitalize()}",
                    queries.format_size(f.size_bytes), f.place_name,
                ])
                # Stash the path on the row so the right-click menu can use it.
                child.setData(0, Qt.UserRole, f.path)
            for server in movie.servers:
                QTreeWidgetItem(top, [f"Available on {server}", "", "☁ Plex", "", server])

        self.tree.expandAll()
        self.tree.setUpdatesEnabled(True)

    def _update_info(self, results):
        summary = queries.summarize(results)
        scope = self.scope_key()
        total = queries.count_movies(self.conn, scope)
        scope_noun = {"all": "movies", "watched": "watched movies", "watchlist": "watchlist movies"}[scope]
        lines = [
            f"<b>{summary.movies}</b> of {total} {scope_noun} shown",
            f"<b>{summary.files}</b> {'file' if summary.files == 1 else 'files'} &middot; "
            f"<b>{queries.format_size(summary.total_bytes) or '0 MB'}</b>",
        ]
        if summary.per_place:
            lines.append("<br><i>Per drive / share:</i>")
            ordered = sorted(summary.per_place.items(), key=lambda kv: -kv[1][1])
            for name, (count, size) in ordered:
                noun = "file" if count == 1 else "files"
                lines.append(f"{name} &mdash; {count} {noun}, {queries.format_size(size) or '0 MB'}")
        self.info_label.setText("<br>".join(lines))
        self._set_status(f"Showing {summary.movies} of {total} {scope_noun} ({summary.files} files)")

    def scope_key(self):
        """'all', 'watched' or 'watchlist', from the tab that's selected."""
        return self.scope_keys[self.scope_bar.currentIndex()]

    def _set_status(self, text):
        """The normal status bar message (what's being shown)."""
        self._status_text = text
        self.statusBar().showMessage(text)

    def _flash_status(self, text, milliseconds=8000):
        """Show a message for a few seconds, then go back to the normal one."""
        self.statusBar().showMessage(text)
        QTimer.singleShot(milliseconds, lambda: self.statusBar().showMessage(self._status_text))

    # ------------------------------------------------------------------
    # Toolbar and long-running tasks (rescan / refresh)
    # ------------------------------------------------------------------
    def _build_toolbar(self):
        """Dolphin-style buttons: an icon with its name underneath."""
        self.toolbar = QToolBar("Library")
        self.toolbar.setMovable(False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.toolbar.setIconSize(QSize(TOOLBAR_ICON_SIZE, TOOLBAR_ICON_SIZE))
        style = self.style()  # built-in icons, so they match the platform

        self.rescan_action = QAction(style.standardIcon(QStyle.SP_DriveHDIcon), "Rescan Drives", self)
        self.rescan_action.setToolTip("Search your drives for every movie in the list")
        self.rescan_action.triggered.connect(self.rescan_drives)

        self.plex_action = QAction(style.standardIcon(QStyle.SP_DriveNetIcon), "Refresh Plex", self)
        self.plex_action.setToolTip("Check your friends' Plex servers for your movies")
        self.plex_action.triggered.connect(self.refresh_plex)

        self.toolbar.addAction(self.rescan_action)
        self.toolbar.addAction(self.plex_action)
        self.addToolBar(Qt.TopToolBarArea, self.toolbar)

        # The same actions in a menu, for keyboard and screen-reader users.
        library_menu = self.menuBar().addMenu("&Library")
        library_menu.addAction(self.rescan_action)
        library_menu.addAction(self.plex_action)

        # Progress widgets on the right of the status bar, hidden until needed.
        self.task_label = QLabel()
        self.task_progress = QProgressBar()
        self.task_progress.setMaximumWidth(220)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_task)
        for widget in (self.task_label, self.task_progress, self.cancel_button):
            self.statusBar().addPermanentWidget(widget)
            widget.hide()

    def _confirm_rescan(self):
        """Ask 'are you sure?' unless the last scan was quick."""
        last = self.last_scan_seconds
        if last is not None and last < CONFIRM_RESCAN_IF_SLOWER_THAN:
            return True
        detail = (f"The last scan took about {last:.0f} seconds."
                  if last else "This can take a minute or more.")
        answer = QMessageBox.question(
            self, "Rescan drives",
            f"Rescan all drives for your movies?\n\n{detail}",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        return answer == QMessageBox.Yes

    def rescan_drives(self):
        if self._thread is not None or not self._confirm_rescan():
            return
        self._start_task(
            "Rescanning drives",
            lambda progress, note, cancel: scanner.run_scan(progress=progress, cancel_event=cancel),
            self._rescan_done,
        )

    def refresh_plex(self):
        if self._thread is not None:
            return

        def task(progress, note, cancel):
            # Imported here so the app still opens if the Plex pieces
            # (the `requests` package, config.json) aren't set up yet.
            import remote_scanner
            return remote_scanner.run_remote_refresh(progress=progress, log=note, interactive=False)

        self._start_task("Refreshing Plex", task, self._plex_done)

    def _rescan_done(self, seconds):
        self.last_scan_seconds = seconds
        self._after_data_change()
        self._flash_status(f"Rescan finished in {seconds:.0f} seconds.")

    def _plex_done(self, summary):
        self._after_data_change()
        self._flash_status(
            f"Plex refresh finished: {summary['matches']} matches on {summary['servers']} servers."
        )

    def _after_data_change(self):
        """New data is in the database: rebuild the sidebar and the list."""
        self._rebuild_filter_sections()
        self.refresh()
        self.schedule_save()

    def _start_task(self, label, task, on_success):
        """Run `task` on a background thread and show its progress."""
        self._task_text = label
        self._on_success = on_success
        self._task_notes = []

        self._thread = QThread(self)
        self._worker = TaskWorker(task)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_task_progress)
        self._worker.note.connect(self._on_task_note)
        self._worker.finished.connect(self._on_task_finished)

        self._set_task_ui(running=True)
        self._thread.start()

    def _set_task_ui(self, running):
        self.rescan_action.setEnabled(not running)
        self.plex_action.setEnabled(not running)
        self.task_label.setText(f"{self._task_text}..." if running else "")
        self.task_progress.setRange(0, 0)  # "busy" animation until real numbers arrive
        for widget in (self.task_label, self.task_progress, self.cancel_button):
            widget.setVisible(running)
        self.cancel_button.setEnabled(running)

    def _on_task_progress(self, done, total, message):
        self.task_progress.setRange(0, max(total, 1))
        self.task_progress.setValue(done)
        self.task_label.setText(f"{message}: {done} of {total}")

    def _on_task_note(self, text):
        self._task_notes.append(text)

    def _cancel_task(self):
        if self._worker is not None:
            self._worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.task_label.setText("Cancelling...")

    def _on_task_finished(self, result, error):
        # Let the thread finish cleanly before dropping our references.
        self._thread.quit()
        self._thread.wait()
        self._thread.deleteLater()
        self._worker.deleteLater()
        self._thread = None
        self._worker = None
        self._set_task_ui(running=False)

        if error is None:
            self._on_success(result)
        elif isinstance(error, scanner.ScanCancelled):
            self._flash_status("Cancelled. Nothing was changed.")
        else:
            QMessageBox.warning(self, self._task_text, f"{self._task_text} failed:\n\n{error}")

        if self._task_notes:
            QMessageBox.information(self, self._task_text, "\n".join(self._task_notes))

    # ------------------------------------------------------------------
    # Folder exclusions
    # ------------------------------------------------------------------
    def _add_excluded_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder to exclude")
        if folder and folder not in self._excluded_folders():
            self.exclude_list.addItem(folder)
            self.schedule_refresh()

    def _remove_excluded_folders(self):
        for item in self.exclude_list.selectedItems():
            self.exclude_list.takeItem(self.exclude_list.row(item))
        self.schedule_refresh()

    # ------------------------------------------------------------------
    # Right-click menu on a file row
    # ------------------------------------------------------------------
    def _show_context_menu(self, position):
        item = self.tree.itemAt(position)
        if item is None:
            return
        file_path = item.data(0, Qt.UserRole)       # set on file rows
        movie = item.data(0, Qt.UserRole + 1)       # (title, year) on movie rows

        menu = QMenu(self)
        if movie:
            title, year, letterboxd_link = movie
            copy_title = menu.addAction("Copy title")
            duckduckgo = menu.addAction("Search DuckDuckGo")
            letterboxd = menu.addAction("Find on Letterboxd")
            chosen = menu.exec(self.tree.viewport().mapToGlobal(position))
            if chosen == copy_title:
                QGuiApplication.clipboard().setText(title)
            elif chosen == duckduckgo:
                QDesktopServices.openUrl(QUrl(duckduckgo_url(title, year)))
            elif chosen == letterboxd:
                # The exact film page when we have its Letterboxd link,
                # otherwise the best-effort search.
                QDesktopServices.openUrl(QUrl(letterboxd_link or letterboxd_url(title, year)))
        elif file_path:
            reveal_action = menu.addAction("Open containing folder")
            copy_action = menu.addAction("Copy path")
            open_action = menu.addAction("Open file")
            chosen = menu.exec(self.tree.viewport().mapToGlobal(position))
            if chosen == reveal_action:
                reveal_in_file_manager(file_path)
            elif chosen == copy_action:
                QGuiApplication.clipboard().setText(file_path)
            elif chosen == open_action:
                QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))
        # Plex rows have nothing to act on, so no menu appears for them.

    # ------------------------------------------------------------------
    # Zoom (View menu, Ctrl + / Ctrl - / Ctrl 0, Ctrl + mouse wheel)
    # ------------------------------------------------------------------
    def _build_view_menu(self):
        view_menu = self.menuBar().addMenu("&View")

        zoom_in = QAction("Zoom &In", self)
        # Ctrl++ needs Shift on many keyboards, so Ctrl+= works as well.
        zoom_in.setShortcuts([QKeySequence(QKeySequence.ZoomIn), QKeySequence("Ctrl+=")])
        zoom_in.triggered.connect(self.zoom_in)

        zoom_out = QAction("Zoom &Out", self)
        zoom_out.setShortcut(QKeySequence(QKeySequence.ZoomOut))
        zoom_out.triggered.connect(self.zoom_out)

        zoom_reset = QAction("&Reset Zoom", self)
        zoom_reset.setShortcut(QKeySequence("Ctrl+0"))
        zoom_reset.triggered.connect(lambda: self.set_zoom(1.0))

        for action in (zoom_in, zoom_out, zoom_reset):
            view_menu.addAction(action)

    def zoom_in(self):
        self.set_zoom(self.zoom_factor + ZOOM_STEP)

    def zoom_out(self):
        self.set_zoom(self.zoom_factor - ZOOM_STEP)

    def set_zoom(self, factor):
        """Scale every font in the program to `factor` x the normal size."""
        self.zoom_factor = round(max(ZOOM_MIN, min(ZOOM_MAX, factor)), 2)

        font = QFont(self.base_font)
        font.setPointSizeF(self.base_font.pointSizeF() * self.zoom_factor)
        QApplication.setFont(font)  # updates every widget without its own font

        # The search box has its own, larger font, so it's resized by hand.
        search_font = QFont(font)
        search_font.setPointSizeF(font.pointSizeF() * SEARCH_FONT_SCALE)
        self.search_box.setFont(search_font)
        self.search_box.setFixedHeight(int(QFontMetrics(search_font).height() * 1.8))

        # Toolbar icons grow with the text.
        icon_size = int(TOOLBAR_ICON_SIZE * self.zoom_factor)
        self.toolbar.setIconSize(QSize(icon_size, icon_size))

        # Keep the sidebar wide enough for the larger text.
        self.filter_scroll.setMinimumWidth(int(270 * self.zoom_factor))

        # Movie rows have their own (bold) font, so resize it by hand. The
        # tree itself is NOT rebuilt, so your expanded/collapsed rows and
        # scroll position stay exactly as they were.
        bold = QFont(self.tree.font())
        bold.setBold(True)
        for index in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(index).setFont(0, bold)

        self.schedule_save()

    def eventFilter(self, obj, event):
        """Turn Ctrl + mouse wheel into zoom, wherever the mouse is."""
        if event.type() == QEvent.Wheel and event.modifiers() & Qt.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            elif event.angleDelta().y() < 0:
                self.zoom_out()
            return True  # we handled it; don't also scroll the list
        return super().eventFilter(obj, event)


def duckduckgo_url(title, year):
    """A plain DuckDuckGo search for the movie."""
    query = f"{title} {year or ''} film".strip()
    return f"https://duckduckgo.com/?q={quote_plus(query)}"


def letterboxd_url(title, year):
    """Try to land directly on the movie's Letterboxd page.

    Letterboxd's own search page is awkward, so this uses DuckDuckGo's
    "!ducky" shortcut, which jumps straight to the first search result,
    restricted to Letterboxd film pages. It's a best effort: an unusual
    title can land on the wrong film. (Storing each movie's Letterboxd
    link from your export would make this exact; that's a later upgrade.)
    """
    query = f'!ducky site:letterboxd.com/film "{title}" {year or ""}'.strip()
    return f"https://duckduckgo.com/?q={quote_plus(query)}"


def reveal_in_file_manager(path):
    """Show the file's folder, highlighting the file where possible."""
    if sys.platform == "win32":
        # Explorer wants /select,"full path" as ONE string with the path in
        # quotes, so we pass a string rather than a list.
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))


def main():
    if not DB_PATH.exists():
        sys.exit("movies.db not found. Run database.py and scanner.py first.")

    # Read-only connection: the UI can browse but never modify the data.
    conn = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)

    app = QApplication(sys.argv)
    window = MainWindow(conn)
    window.resize(1250, 800)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()