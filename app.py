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
read-only for browsing. The only things the UI ever writes are your
confirmations (Confirm menu, toolbar, Space key, right-click), and those go
through links.py on a short-lived separate connection.

Tabs above the list: All / Watched / Watchlist / Confirmed / Unmatched files.

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
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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

import links
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
CONFIRMED_LABEL = "✔ Confirmed"

# Tabs that don't use the Confidence checkboxes.
SCOPES_WITHOUT_CONFIDENCE = ("confirmed", "unmatched")

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


class LinkMovieDialog(QDialog):
    """Pick which movie a file belongs to, with a type-ahead search.

    Type any words of the title (any order, accents ignored); Up/Down move
    through the matches while the cursor stays in the search box, and Enter
    accepts. selected_movie() then gives (movie_id, title, year).
    """

    def __init__(self, parent, file_label, choices, initial_text):
        super().__init__(parent)
        self.setWindowTitle("Link to a movie")
        self.choices = choices

        label = QLabel(file_label)
        label.setTextFormat(Qt.PlainText)  # file names can contain < and &
        label.setWordWrap(True)

        self.search = QLineEdit(initial_text)
        self.search.setPlaceholderText("Type part of the movie's title...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refilter)
        self.search.installEventFilter(self)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Link")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(self.search)
        layout.addWidget(self.list, stretch=1)
        layout.addWidget(self.buttons)
        self.resize(520, 480)

        self._refilter()
        self.search.setFocus()
        self.search.selectAll()

    def _refilter(self):
        self.list.clear()
        for choice in queries.filter_choices(self.choices, self.search.text()):
            text = f"{choice.title} ({choice.year})" if choice.year else choice.title
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, (choice.movie_id, choice.title, choice.year))
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(self.list.count() > 0)

    def eventFilter(self, obj, event):
        """Let the arrow keys drive the list while typing in the search box."""
        if (obj is self.search and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_PageUp)):
            QApplication.sendEvent(self.list, event)
            return True
        return super().eventFilter(obj, event)

    def selected_movie(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None


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
        self.results = []              # the movies currently listed (for auto-confirm)
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
        # Shift/Ctrl-click several rows to confirm (or link) them together.
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        # Enter / double-click on a row in the Unmatched files tab opens the
        # "link to a movie" picker.
        self.tree.itemActivated.connect(self._on_item_activated)
        # Space confirms / un-confirms the selected file rows. It is tied to
        # the tree itself (not the whole window) so typing a space in the
        # search box still works.
        self.confirm_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self.tree)
        self.confirm_shortcut.setContext(Qt.WidgetShortcut)
        self.confirm_shortcut.activated.connect(self.toggle_confirm_selected)
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
        self.confidence_section.add_checkbox("✔ Confirmed", True, "confirmed", key="confirmed")
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
        scope = self.scope_key()
        # The Confirmed and Unmatched tabs ignore the Confidence boxes, and
        # Unmatched is a list of files, so sorting by movie doesn't apply.
        self.confidence_section.setEnabled(scope not in SCOPES_WITHOUT_CONFIDENCE)
        self.sort_box.setEnabled(scope != "unmatched")

        if scope == "unmatched":
            self.results = []
            files = queries.fetch_unmatched(
                self.conn,
                search=self.search_box.text(),
                local_place_ids=self._checked_place_ids(["local", "network"]),
                excluded_folders=self._excluded_folders(),
            )
            self._fill_unmatched_tree(files)
            self._update_info_unmatched(files)
            return

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
            scope=scope,
        )
        self.results = results
        self._fill_tree(results)
        self._update_info(results)

    def _all_tree_items(self):
        """Every row in the tree in on-screen order (movies, then their files)."""
        items = []
        for index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(index)
            items.append(top)
            items.extend(top.child(i) for i in range(top.childCount()))
        return items

    def refresh_keep_position(self):
        """Refresh, then put the scroll position and selected row back.

        A plain refresh() rebuilds the tree and jumps to the top. After
        confirming something that would be annoying when you're working
        down a long list, so this restores where you were. If the selected
        row disappeared (a confirmed file leaving the list because the
        Confirmed box is unticked), the row now in its place is selected,
        which is the next one down.
        """
        scrollbar = self.tree.verticalScrollBar()
        scroll = scrollbar.value()
        items = self._all_tree_items()
        current = self.tree.currentItem()
        position = items.index(current) if current in items else None

        self.refresh()

        items = self._all_tree_items()
        if position is not None and items:
            self.tree.setCurrentItem(items[min(position, len(items) - 1)])
        scrollbar.setValue(scroll)

    def _fill_tree(self, results):
        # Turning off repainting while we add thousands of rows is much faster.
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        bold = QFont(self.tree.font())
        bold.setBold(True)
        sort_name = self.sort_box.currentText()
        # Column titles (the Unmatched tab uses different ones). In the
        # Watchlist scope the date column means "added to watchlist".
        date_title = "Added" if self.scope_key() == "watchlist" else "Logged"
        for column, title in enumerate(("Movie / File", "Year", "Match", "Size", "Place", date_title)):
            self.tree.headerItem().setText(column, title)

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
            elif any(f.confirmed for f in movie.files):
                top.setText(2, CONFIRMED_LABEL)

            for f in movie.files:
                label = CONFIRMED_LABEL if f.confirmed else (
                    f"{CONFIDENCE_ICONS[f.confidence]} {f.confidence.capitalize()}"
                )
                child = QTreeWidgetItem(top, [
                    f.path, "", label,
                    queries.format_size(f.size_bytes), f.place_name,
                ])
                # Stash the path on the row so the right-click menu can use it,
                # and (movie, path, size, confirmed) for confirming it.
                child.setData(0, Qt.UserRole, f.path)
                child.setData(0, Qt.UserRole + 2, (movie.movie_id, f.path, f.size_bytes, f.confirmed))
            for server in movie.servers:
                QTreeWidgetItem(top, [f"Available on {server}", "", "☁ Plex", "", server])

        self.tree.expandAll()
        self.tree.setUpdatesEnabled(True)

    def _update_info(self, results):
        summary = queries.summarize(results)
        scope = self.scope_key()
        total = queries.count_movies(self.conn, scope)
        scope_noun = {
            "all": "movies",
            "watched": "watched movies",
            "watchlist": "watchlist movies",
            "confirmed": "confirmed movies",
        }[scope]
        confirmed_total = queries.count_movies(self.conn, "confirmed")
        lines = [
            f"<b>{summary.movies}</b> of {total} {scope_noun} shown",
            f"✔ <b>{confirmed_total}</b> confirmed",
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

    def _fill_unmatched_tree(self, files):
        """The Unmatched files tab: one flat row per file nobody has claimed."""
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        for column, title in enumerate(("File", "", "", "Size", "Place", "")):
            self.tree.headerItem().setText(column, title)
        for f in files:
            item = QTreeWidgetItem([f.path, "", "", queries.format_size(f.size_bytes), f.place_name, ""])
            item.setToolTip(0, f.path)
            item.setData(0, Qt.UserRole, f.path)                      # right-click menu
            item.setData(0, Qt.UserRole + 3, (f.path, f.size_bytes))  # linking it
            self.tree.addTopLevelItem(item)
        self.tree.setUpdatesEnabled(True)

    def _update_info_unmatched(self, files):
        if not queries.has_file_catalog(self.conn):
            self.info_label.setText(
                "No file list yet. Run <b>Rescan Drives</b> once to build it."
            )
            self._set_status("Unmatched files: rescan your drives to build the file list")
            return
        total_bytes = sum(f.size_bytes or 0 for f in files)
        per_place = {}
        for f in files:
            count, size = per_place.get(f.place_name, (0, 0))
            per_place[f.place_name] = (count + 1, size + (f.size_bytes or 0))
        noun = "file" if len(files) == 1 else "files"
        lines = [
            f"<b>{len(files)}</b> unmatched {noun}",
            f"<b>{queries.format_size(total_bytes) or '0 MB'}</b>",
            "<i>Select a file and press Space (or double-click) to link it to a movie.</i>",
        ]
        if per_place:
            lines.append("<br><i>Per drive / share:</i>")
            for name, (count, size) in sorted(per_place.items(), key=lambda kv: -kv[1][1]):
                lines.append(f"{name} &mdash; {count}, {queries.format_size(size) or '0 MB'}")
        self.info_label.setText("<br>".join(lines))
        self._set_status(f"{len(files)} unmatched {noun}")

    def scope_key(self):
        """The key of the selected tab: 'all', 'watched', 'watchlist', 'confirmed' or 'unmatched'."""
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

        # Auto-confirm buttons (each one asks "are you sure?" first).
        self.confirm_strong_action = QAction(
            style.standardIcon(QStyle.SP_DialogApplyButton), "Confirm Strong", self
        )
        self.confirm_strong_action.setToolTip(
            "Confirm every Strong match that has exactly one candidate file"
        )
        self.confirm_strong_action.triggered.connect(lambda: self.auto_confirm("strong"))

        self.confirm_basic_action = QAction(
            style.standardIcon(QStyle.SP_DialogApplyButton), "Confirm Basic", self
        )
        self.confirm_basic_action.setToolTip(
            "Confirm every Basic match that has exactly one candidate file"
        )
        self.confirm_basic_action.triggered.connect(lambda: self.auto_confirm("basic"))

        self.toolbar.addAction(self.rescan_action)
        self.toolbar.addAction(self.plex_action)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.confirm_strong_action)
        self.toolbar.addAction(self.confirm_basic_action)
        self.addToolBar(Qt.TopToolBarArea, self.toolbar)

        # The same actions in a menu, for keyboard and screen-reader users.
        library_menu = self.menuBar().addMenu("&Library")
        library_menu.addAction(self.rescan_action)
        library_menu.addAction(self.plex_action)

        # No shortcut is attached to this menu item: a window-wide Space
        # would steal spaces typed in the search box. The Space key is wired
        # to the list itself instead (see _build_ui).
        confirm_menu = self.menuBar().addMenu("&Confirm")
        toggle_action = QAction("Confirm / Remove Confirmation on Selected (Space)", self)
        toggle_action.triggered.connect(self.toggle_confirm_selected)
        confirm_menu.addAction(toggle_action)
        confirm_menu.addSeparator()
        confirm_menu.addAction(self.confirm_strong_action)
        confirm_menu.addAction(self.confirm_basic_action)

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
    # Confirming files and linking unmatched ones
    # ------------------------------------------------------------------
    def _write_links(self, action):
        """Run a database change through links.py. Returns (ok, result)."""
        try:
            return True, links.run_write(DB_PATH, action)
        except sqlite3.Error as error:
            QMessageBox.warning(
                self, "Couldn't save",
                f"The change wasn't saved:\n\n{error}\n\n"
                "If a rescan is saving right now, wait a moment and try again.",
            )
            return False, None

    def _selected_file_rows(self):
        """Selected file rows as (movie_id, path, size, confirmed) tuples.

        A selected movie row counts as its file when it has exactly one;
        a movie with several files makes you pick the one you mean.
        """
        rows = []
        for item in self.tree.selectedItems():
            info = item.data(0, Qt.UserRole + 2)
            if info:
                rows.append(info)
            elif item.data(0, Qt.UserRole + 1):  # a movie row
                children = [item.child(i).data(0, Qt.UserRole + 2) for i in range(item.childCount())]
                children = [c for c in children if c]
                if len(children) == 1:
                    rows.append(children[0])
        return list(dict.fromkeys(rows))  # no duplicates, same order

    def toggle_confirm_selected(self):
        """Space: confirm the selected files, or un-confirm them if they all
        already are. In the Unmatched files tab it opens the link picker."""
        if self.scope_key() == "unmatched":
            self._link_selected_files()
            return
        rows = self._selected_file_rows()
        if not rows:
            self._flash_status(
                "Select a file row first (for a movie with several files, pick the one you mean)."
            )
            return
        if all(row[3] for row in rows):
            self._set_confirmed(rows, confirm=False)
        else:
            self._set_confirmed([row for row in rows if not row[3]], confirm=True)

    def _set_confirmed(self, rows, confirm):
        """Confirm or un-confirm `rows` ((movie_id, path, size, confirmed) tuples)."""
        items = [(movie_id, path, size) for movie_id, path, size, _ in rows]
        action = links.confirm_files if confirm else links.unconfirm_files
        ok, count = self._write_links(lambda conn: action(conn, items))
        if not ok:
            return
        noun = "file" if count == 1 else "files"
        verb = "Confirmed" if confirm else "Removed confirmation from"
        self.refresh_keep_position()
        self._flash_status(f"{verb} {count} {noun}.")

    def _on_item_activated(self, item, _column):
        """Enter / double-click: in the Unmatched files tab, link that file."""
        if self.scope_key() == "unmatched":
            self._link_selected_files()

    def _link_selected_files(self):
        """Ask which movie the selected unmatched file(s) belong to, then confirm them.

        Several files can be linked to the same movie in one go (a movie
        split over two files, or a movie plus its other-language copy).
        """
        selected = [i.data(0, Qt.UserRole + 3) for i in self.tree.selectedItems()]
        selected = [info for info in selected if info]
        if not selected:
            self._flash_status("Select an unmatched file first.")
            return

        first_path = selected[0][0]
        if len(selected) == 1:
            file_label = first_path
        else:
            file_label = f"{len(selected)} files, starting with:\n{first_path}"
        dialog = LinkMovieDialog(
            self, file_label, queries.load_movie_choices(self.conn), queries.guess_title(first_path)
        )
        if dialog.exec() != QDialog.Accepted:
            return
        chosen = dialog.selected_movie()
        if chosen is None:
            return
        movie_id, title, year = chosen

        items = [(movie_id, path, size) for path, size in selected]
        ok, _ = self._write_links(lambda conn: links.confirm_files(conn, items))
        if not ok:
            return
        self.refresh_keep_position()
        noun = "file" if len(items) == 1 else "files"
        label = f"{title} ({year})" if year else title
        self._flash_status(f"Linked {len(items)} {noun} to {label}.")

    def auto_confirm(self, level):
        """Confirm every unambiguous Strong (or Basic) match in the current list."""
        if self.scope_key() in SCOPES_WITHOUT_CONFIDENCE:
            QMessageBox.information(
                self, "Auto-confirm",
                "Switch to the All, Watched or Watchlist tab first. Auto-confirm "
                "works on the movies listed there.",
            )
            return

        name = level.capitalize()
        plan = links.plan_auto_confirm(self.conn, self.results, level)
        skipped = []
        if plan.skipped_several:
            skipped.append(f"{plan.skipped_several} with several {name} files")
        if plan.skipped_shared:
            skipped.append(f"{plan.skipped_shared} whose file also matches another movie")
        if plan.skipped_done:
            skipped.append(f"{plan.skipped_done} that already have a confirmed file")
        skipped_text = ("Left for you to review: " + ", ".join(skipped) + ".") if skipped else ""

        if not plan.items:
            QMessageBox.information(
                self, "Auto-confirm",
                f"Nothing to auto-confirm: no {name} matches in the current list are "
                f"unambiguous.\n\n{skipped_text}".rstrip(),
            )
            return

        noun = "match" if len(plan.items) == 1 else "matches"
        text = (
            f"Confirm {len(plan.items)} {name} {noun}?\n\n"
            f"Only movies with exactly one {name} file in the current list are "
            "included, so what you see is what gets confirmed."
        )
        if skipped_text:
            text += f"\n\n{skipped_text}"
        answer = QMessageBox.question(
            self, f"Confirm all {name}", text, QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel
        )
        if answer != QMessageBox.Yes:
            return

        ok, count = self._write_links(lambda conn: links.confirm_files(conn, plan.items))
        if ok:
            self.refresh_keep_position()
            self._flash_status(f"Confirmed {count} {name} {'match' if count == 1 else 'matches'}.")

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
            confirm_info = item.data(0, Qt.UserRole + 2)    # (movie_id, path, size, confirmed)
            unmatched_info = item.data(0, Qt.UserRole + 3)  # (path, size), Unmatched tab only
            confirm_action = link_action = None
            if confirm_info:
                confirm_action = menu.addAction(
                    "Remove confirmation" if confirm_info[3] else "Confirm this file"
                )
            if unmatched_info:
                link_action = menu.addAction("Link to movie...")
            if confirm_action or link_action:
                menu.addSeparator()
            reveal_action = menu.addAction("Open containing folder")
            copy_action = menu.addAction("Copy path")
            open_action = menu.addAction("Open file")
            chosen = menu.exec(self.tree.viewport().mapToGlobal(position))
            if chosen is None:
                return
            if chosen == confirm_action:
                self._set_confirmed([confirm_info], confirm=not confirm_info[3])
            elif chosen == link_action:
                if not item.isSelected():
                    self.tree.setCurrentItem(item)
                self._link_selected_files()
            elif chosen == reveal_action:
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
        if self.scope_key() != "unmatched":  # there, top-level rows are files
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

    # Create the confirmation tables if an older movies.db doesn't have
    # them yet (nothing else is touched), then open the connection the UI
    # browses with. It is read-only; confirmations are saved through
    # links.py on separate, short-lived connections.
    links.ensure_database(DB_PATH)
    conn = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)

    app = QApplication(sys.argv)
    window = MainWindow(conn)
    window.resize(1250, 800)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()