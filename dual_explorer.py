"""
DualExplorer - Двухпанельный файловый менеджер с AI-ассистентом

Архитектура приложения:
======================

1. FileViewer / ImageViewer
   - Диалоги для просмотра и редактирования файлов
   - FileViewer: текстовые файлы (UTF-8, CP1251), режимы просмотра/редактирования
   - ImageViewer: изображения с масштабированием (+/-, 100%)

2. FilePanel
   - Панель для отображения файлов и папок
   - Функции: навигация, поиск, сортировка, drag-and-drop
   - Кэширование списка файлов (all_items) для быстрого поиска
   - Хранение полных путей в Qt.ItemDataRole.UserRole

3. DualExplorerWindow
   - Главное окно с двумя FilePanel (левая и правая)
   - Управление активной панелью (active_panel)
   - Горячие клавиши F2-F8, Ctrl+R
   - Темы оформления (светлая/тёмная)

4. ChatDatabase
   - SQLite база данных для истории AI-диалогов
   - Хранит: запросы, ответы, действия, параметры, статусы, ошибки
   - Используется для формирования контекста (последние 6 диалогов)

5. AIDialog
   - AI-ассистент на базе DeepSeek API
   - Асинхронные запросы через threading.Thread
   - Межпоточное взаимодействие через pyqtSignal
   - Выполнение команд: copy, move, delete, create_folder, create_file, rename
   - Поддержка паттернов (*.txt, file?.doc) через fnmatch

6. StartupDialog
   - Диалог выбора начальных директорий при запуске

Технологии:
===========
- PyQt6: GUI фреймворк
- SQLite3: база данных для истории
- OpenAI SDK: работа с DeepSeek API
- threading: асинхронные операции
- os/shutil/pathlib: файловые операции
- fnmatch: паттерны для файлов

Ключевые особенности:
====================
- Drag-and-drop между панелями (QMimeData, QDrag)
- Поиск в реальном времени (без перечитывания диска)
- AI с контекстом (помнит последние 6 диалогов)
- Обработка ошибок (try-except, QMessageBox)
- Кросс-платформенность (Windows, macOS, Linux)
"""

from PyQt6.QtGui import QKeySequence, QShortcut, QDrag, QDesktopServices, QPixmap
from PyQt6.QtCore import Qt, QUrl, QMimeData, QSize, QFileInfo, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QListWidget, QListWidgetItem, QPushButton,
                             QLabel, QMessageBox, QInputDialog, QTextEdit, QDialog,
                             QFileDialog, QFileIconProvider, QMenu, QLineEdit, QComboBox,
                             QScrollArea)
import sys
import os
import shutil
import json
import sqlite3
from pathlib import Path
from datetime import datetime

# Устранение предупреждений macOS TSMSendMessageToUIServer, только при запуске на macOS
os.environ['QT_MAC_WANTS_LAYER'] = '1'


# ============================================================================
# Классы для просмотра и редактирования файлов
# ============================================================================

class FileViewer(QDialog):
    """
    Просмотр и редактирование текстовых файлов

    Универсальный диалог для работы с текстовыми файлами.
    Поддерживает два режима:
    - edit_mode=False: только чтение (F3)
    - edit_mode=True: редактирование с сохранением (F4)

    Особенности:
    - Автоопределение кодировки (UTF-8, fallback на CP1251)
    - Горячие клавиши: Ctrl+S (сохранить), ESC (закрыть)
    - Модальное окно (блокирует главное окно)
    """

    def __init__(self, file_path, parent=None, edit_mode=False):
        super().__init__(parent)
        # Полный путь к файлу
        self.file_path = file_path
        # Режим: False=просмотр, True=редактирование
        self.edit_mode = edit_mode
        self.init_ui()
        # Загружаем содержимое файла сразу при создании
        self.load_file()

    def init_ui(self):
        mode_text = 'Редактирование' if self.edit_mode else 'Просмотр'
        self.setWindowTitle(f'{mode_text}: {os.path.basename(self.file_path)}')
        self.setGeometry(200, 200, 800, 600)

        layout = QVBoxLayout()
        self.setLayout(layout)

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(not self.edit_mode)
        layout.addWidget(self.text_edit)

        buttons_layout = QHBoxLayout()

        if self.edit_mode:
            save_btn = QPushButton('Сохранить (Ctrl+S)')
            save_btn.clicked.connect(self.save_file)
            buttons_layout.addWidget(save_btn)

            self.save_shortcut = QShortcut(QKeySequence(
                Qt.Modifier.CTRL | Qt.Key.Key_S), self)
            self.save_shortcut.activated.connect(self.save_file)

        close_btn = QPushButton('Закрыть (ESC)')
        close_btn.clicked.connect(self.close)
        buttons_layout.addWidget(close_btn)

        layout.addLayout(buttons_layout)

        self.esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.esc_shortcut.activated.connect(self.close)

    def load_file(self):
        """Загрузка файла"""
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                self.text_edit.setPlainText(content)
        except Exception as e:
            self.text_edit.setPlainText(f'Ошибка чтения файла: {str(e)}')

    def save_file(self):
        try:
            content = self.text_edit.toPlainText()
            with open(self.file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            QMessageBox.information(self, 'Успех', 'Файл успешно сохранен')
        except Exception as e:
            QMessageBox.critical(
                self, 'Ошибка', f'Не удалось сохранить файл: {str(e)}')


class ImageViewer(QDialog):
    """
    Просмотр изображений с возможностью масштабирования

    Диалог для отображения изображений (jpg, png, gif, bmp и т.д.)

    Возможности:
    - Масштабирование: +/- (увеличить/уменьшить), 100% (сброс)
    - Прокрутка через QScrollArea для больших изображений
    - Плавное масштабирование (Qt.TransformationMode.SmoothTransformation)
    - Горячие клавиши: +, -, ESC

    Технические детали:
    - Использует QPixmap для загрузки и отображения
    - Хранит оригинальный pixmap для качественного масштабирования
    - Коэффициент масштабирования (scale_factor) изменяется на 20% за шаг
    """

    def __init__(self, file_path, parent=None):
        super().__init__(parent)
        # Полный путь к файлу изображения
        self.file_path = file_path
        self.init_ui()
        # Загружаем изображение сразу при создании
        self.load_image()

    def init_ui(self):
        self.setWindowTitle(f'Просмотр: {os.path.basename(self.file_path)}')
        self.setGeometry(200, 200, 800, 600)

        layout = QVBoxLayout()
        self.setLayout(layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(scroll)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setScaledContents(False)
        scroll.setWidget(self.image_label)

        buttons_layout = QHBoxLayout()

        zoom_in_btn = QPushButton('Увеличить (+)')
        zoom_in_btn.clicked.connect(self.zoom_in)
        buttons_layout.addWidget(zoom_in_btn)

        zoom_out_btn = QPushButton('Уменьшить (-)')
        zoom_out_btn.clicked.connect(self.zoom_out)
        buttons_layout.addWidget(zoom_out_btn)

        reset_btn = QPushButton('100%')
        reset_btn.clicked.connect(self.reset_zoom)
        buttons_layout.addWidget(reset_btn)

        close_btn = QPushButton('Закрыть (ESC)')
        close_btn.clicked.connect(self.close)
        buttons_layout.addWidget(close_btn)

        layout.addLayout(buttons_layout)

        self.esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.esc_shortcut.activated.connect(self.close)

        self.plus_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Plus), self)
        self.plus_shortcut.activated.connect(self.zoom_in)

        self.minus_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Minus), self)
        self.minus_shortcut.activated.connect(self.zoom_out)

    def load_image(self):
        self.original_pixmap = QPixmap(self.file_path)
        if self.original_pixmap.isNull():
            QMessageBox.critical(
                self, 'Ошибка', 'Не удалось загрузить изображение')
            self.close()
            return
        self.zoom_level = 1.0
        self.update_image()

    def update_image(self):
        """Обновление отображения с учетом масштаба"""
        if not self.original_pixmap.isNull():
            scaled_pixmap = self.original_pixmap.scaled(
                self.original_pixmap.size() * self.zoom_level,
                Qt.AspectRatioMode.KeepAspectRatio,  # Сохранение пропорций
                Qt.TransformationMode.SmoothTransformation
            )
            self.image_label.setPixmap(scaled_pixmap)

    def zoom_in(self):
        self.zoom_level *= 1.2
        self.update_image()

    def zoom_out(self):
        self.zoom_level /= 1.2
        self.update_image()

    def reset_zoom(self):
        self.zoom_level = 1.0
        self.update_image()


# ============================================================================
# Панель файлов
# ============================================================================

class FilePanel(QWidget):
    """
    Панель для отображения и управления файлами/папками

    Это один из двух основных компонентов интерфейса (левая или правая панель).
    Каждая панель независима и может отображать разные директории.

    Ключевые особенности:
    - Хранит список файлов в памяти (all_items) для быстрого поиска
    - Использует Qt.ItemDataRole.UserRole для хранения полных путей
    - Поддерживает drag-and-drop через переопределение методов QListWidget
    - Взаимодействует с другой панелью через parent_window
    """

    def __init__(self, start_path=None, parent_window=None):
        super().__init__()
        # Путь к текущей директории (по умолчанию - домашняя папка)
        self.current_path = start_path or str(Path.home())
        # Ссылка на DualExplorerWindow для доступа к другой панели
        self.parent_window = parent_window
        # Режим сортировки (name_asc, name_desc, size_asc, size_desc, date_asc, date_desc)
        self.sort_mode = 'name_asc'
        self.init_ui()
        # Загружаем содержимое директории при создании панели
        self.load_directory(self.current_path)

    def init_ui(self):
        """Инициализация пользовательского интерфейса панели"""
        self.setObjectName("file_panel")

        # Основной вертикальный layout с отступами
        layout = QVBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)  # Отступы по краям
        layout.setSpacing(5)  # Расстояние между элементами
        self.setLayout(layout)

        # Разрешаем заливку фона (для выделения активной панели)
        self.setAutoFillBackground(True)

        # === Строка с путём к текущей директории ===
        path_layout = QHBoxLayout()
        path_label = QLabel('📁')  # Иконка папки
        path_layout.addWidget(path_label)

        # Редактируемое поле пути (можно вводить путь вручную)
        self.path_input = QLineEdit(self.current_path)
        self.path_input.returnPressed.connect(
            self.navigate_to_path)  # Enter → переход
        path_layout.addWidget(self.path_input)

        go_btn = QPushButton('→')
        go_btn.setMaximumWidth(40)
        go_btn.setToolTip('Перейти')
        go_btn.clicked.connect(self.navigate_to_path)
        path_layout.addWidget(go_btn)

        layout.addLayout(path_layout)

        control_layout = QHBoxLayout()

        search_label = QLabel('🔍')
        control_layout.addWidget(search_label)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('Поиск...')
        self.search_input.textChanged.connect(self.filter_files)
        control_layout.addWidget(self.search_input)

        clear_search_btn = QPushButton('✖')
        clear_search_btn.setMaximumWidth(30)
        clear_search_btn.setToolTip('Очистить поиск')
        clear_search_btn.clicked.connect(self.clear_search)
        control_layout.addWidget(clear_search_btn)

        sort_label = QLabel('⇅')
        control_layout.addWidget(sort_label)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            'Имя ↑', 'Имя ↓',
            'Размер ↑', 'Размер ↓',
            'Дата ↑', 'Дата ↓'
        ])
        self.sort_combo.currentIndexChanged.connect(self.change_sort)
        control_layout.addWidget(self.sort_combo)

        layout.addLayout(control_layout)

        self.file_list = QListWidget()
        self.file_list.itemDoubleClicked.connect(self.item_double_clicked)
        self.file_list.setDragEnabled(True)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDropIndicatorShown(True)
        self.file_list.setDragDropMode(QListWidget.DragDropMode.DragDrop)
        self.file_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.file_list.setIconSize(QSize(20, 20))  # Размер иконок
        self.file_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(
            self.show_context_menu)

        # Переопределяем методы drag-and-drop для кастомной обработки
        # Это позволяет перетаскивать файлы между панелями
        self.file_list.startDrag = self.start_drag
        self.file_list.dragEnterEvent = self.drag_enter_event
        self.file_list.dragMoveEvent = self.drag_move_event
        self.file_list.dropEvent = self.drop_event

        layout.addWidget(self.file_list)

        self.icon_provider = QFileIconProvider()
        self.folder_icon = self.icon_provider.icon(QFileInfo(str(Path.home())))
        self.file_icon = self.icon_provider.icon(
            QFileIconProvider.IconType.File)

        self.all_items = []

    def get_icon_for_file(self, file_path):
        file_info = QFileInfo(file_path)
        icon = self.icon_provider.icon(file_info)
        if not icon.isNull():
            return icon
        return self.file_icon

    def navigate_to_path(self):
        path = self.path_input.text()
        if os.path.exists(path) and os.path.isdir(path):
            self.load_directory(path)
        else:
            QMessageBox.warning(
                self, 'Ошибка', f'Путь не существует или не является директорией:\n{path}')
            self.path_input.setText(self.current_path)

    def change_sort(self, index):
        sort_modes = ['name_asc', 'name_desc', 'size_asc',
                      'size_desc', 'date_asc', 'date_desc']
        self.sort_mode = sort_modes[index]
        self.refresh()

    def load_directory(self, path):
        """
        Загрузка содержимого директории с обработкой ошибок

        Это один из самых важных методов FilePanel. Выполняет:
        1. Чтение содержимого директории через os.listdir()
        2. Разделение на папки и файлы
        3. Сортировку согласно self.sort_mode
        4. Создание QListWidgetItem для каждого элемента
        5. Сохранение полных путей в Qt.ItemDataRole.UserRole
        6. Кэширование в self.all_items для быстрого поиска

        Особенности:
        - Папки всегда отображаются выше файлов
        - Элемент ".." для перехода на уровень выше (кроме корня)
        - Try-except для защиты от race conditions (файл удалён между listdir и getsize)
        - Иконки через QFileIconProvider (системные иконки)
        - Размер файлов в человекочитаемом формате (KB, MB, GB)
        """
        try:
            # Нормализуем путь (убираем .., ., двойные слэши)
            self.current_path = os.path.abspath(path)
            self.path_input.setText(self.current_path)
            self.file_list.clear()
            # Очищаем кэш для поиска (будет заполнен заново)
            self.all_items = []
            self.search_input.clear()

            # Добавляем элемент ".." для перехода на уровень выше (если не в корне)
            # Проверка: dirname(path) != path означает, что мы не в корне (/, C:\)
            if os.path.dirname(self.current_path) != self.current_path:
                parent_item = QListWidgetItem(self.folder_icon, '..')
                # Сохраняем путь к родительской директории в UserRole
                parent_item.setData(Qt.ItemDataRole.UserRole,
                                    os.path.dirname(self.current_path))
                self.file_list.addItem(parent_item)
                # Клонируем для кэша поиска
                self.all_items.append(parent_item.clone())

            try:
                entries = os.listdir(self.current_path)
            except PermissionError:
                QMessageBox.warning(
                    self, 'Ошибка', f'Нет доступа к директории: {self.current_path}')
                return

            # Разделяем на папки и файлы для раздельной сортировки
            dirs = []
            files = []

            for entry in entries:
                full_path = os.path.join(self.current_path, entry)
                if os.path.isdir(full_path):
                    dirs.append(entry)
                else:
                    files.append(entry)

            # Сортируем папки и файлы отдельно (папки всегда сверху)
            dirs = self.sort_entries(dirs, is_dir=True)
            files = self.sort_entries(files, is_dir=False)

            # Добавляем папки в список
            for dir_name in dirs:
                full_path = os.path.join(self.current_path, dir_name)
                item = QListWidgetItem(self.folder_icon, dir_name)
                # Сохраняем полный путь в UserRole для доступа при операциях
                item.setData(Qt.ItemDataRole.UserRole, full_path)
                self.file_list.addItem(item)
                self.all_items.append(item.clone())  # Клон для поиска

            for file_name in files:
                full_path = os.path.join(self.current_path, file_name)
                try:
                    size = os.path.getsize(full_path)
                    size_str = self.format_size(size)
                    file_icon = self.get_icon_for_file(full_path)
                    item = QListWidgetItem(
                        file_icon, f'{file_name} ({size_str})')
                except:
                    file_icon = self.get_icon_for_file(full_path)
                    item = QListWidgetItem(file_icon, file_name)
                item.setData(Qt.ItemDataRole.UserRole, full_path)
                self.file_list.addItem(item)
                self.all_items.append(item.clone())

        except Exception as e:
            QMessageBox.critical(
                self, 'Ошибка', f'Не удалось загрузить директорию: {str(e)}')

    def sort_entries(self, entries, is_dir=False):
        if self.sort_mode == 'name_asc':
            return sorted(entries, key=str.lower)
        elif self.sort_mode == 'name_desc':
            return sorted(entries, key=str.lower, reverse=True)
        elif self.sort_mode in ['size_asc', 'size_desc']:
            if is_dir:
                return sorted(entries, key=str.lower)

            def get_size(name):
                try:
                    return os.path.getsize(os.path.join(self.current_path, name))
                except:
                    return 0
            return sorted(entries, key=get_size, reverse=(self.sort_mode == 'size_desc'))
        elif self.sort_mode in ['date_asc', 'date_desc']:
            def get_mtime(name):
                try:
                    return os.path.getmtime(os.path.join(self.current_path, name))
                except:
                    return 0
            return sorted(entries, key=get_mtime, reverse=(self.sort_mode == 'date_desc'))
        return entries

    def format_size(self, size):
        for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
            if size < 1024.0:
                return f'{size:.1f} {unit}'
            size /= 1024.0
        return f'{size:.1f} ТБ'

    def item_double_clicked(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        if os.path.isdir(path):
            self.load_directory(path)
        else:
            self.open_file_with_default_app(path)

    def open_file_with_default_app(self, file_path):
        try:
            url = QUrl.fromLocalFile(file_path)
            if not QDesktopServices.openUrl(url):
                QMessageBox.warning(
                    self, 'Ошибка',
                    f'Не удалось открыть файл:\n{file_path}\n\nНет связанного приложения'
                )
        except Exception as e:
            QMessageBox.critical(
                self, 'Ошибка',
                f'Не удалось открыть файл: {str(e)}'
            )

    def get_selected_path(self):
        current_item = self.file_list.currentItem()
        if current_item:
            return current_item.data(Qt.ItemDataRole.UserRole)
        return None

    def refresh(self):
        self.load_directory(self.current_path)

    def filter_files(self):
        """
        Поиск в реальном времени без перечитывания диска

        Ключевая оптимизация: вместо повторного вызова os.listdir() при каждом
        изменении текста в поле поиска, мы фильтруем закэшированный список
        self.all_items, который был создан в load_directory().

        Алгоритм:
        1. Получаем текст поиска (приводим к нижнему регистру)
        2. Очищаем видимый список (file_list)
        3. Если поиск пустой → показываем все элементы из all_items
        4. Если есть текст → проходим по all_items и добавляем только совпадения

        Поиск ведётся:
        - В отображаемом тексте элемента (имя + размер)
        - В чистом имени файла (без размера)

        Преимущества:
        - Мгновенная работа (нет обращений к диску)
        - Работает даже с большими директориями (тысячи файлов)
        - Не нагружает файловую систему
        """
        search_text = self.search_input.text().lower()

        # Очищаем видимый список
        self.file_list.clear()

        # Если поиск пустой - показываем все файлы из кэша
        if not search_text:
            for item in self.all_items:
                # clone() создаёт копию, чтобы не портить оригинал в all_items
                self.file_list.addItem(item.clone())
            return

        # Фильтруем файлы из кэша (all_items)
        for item in self.all_items:
            # Текст элемента (может содержать размер файла)
            item_text = item.text().lower()
            # Извлекаем полный путь из UserRole
            file_path = item.data(Qt.ItemDataRole.UserRole)
            # Получаем чистое имя файла
            file_name = os.path.basename(
                file_path).lower() if file_path else ''

            # Ищем подстроку в отображаемом тексте или в имени файла
            if search_text in item_text or search_text in file_name:
                self.file_list.addItem(item.clone())

    def clear_search(self):
        self.search_input.clear()

    def set_active(self, active):
        if active:
            self.path_input.setStyleSheet("""
                QLineEdit {
                    background-color: #e3f2fd;
                    border: 2px solid #0078d4;
                    border-radius: 3px;
                    padding: 5px;
                    font-weight: bold;
                }
            """)
        else:
            self.path_input.setStyleSheet("")

    def show_context_menu(self, position):
        item = self.file_list.itemAt(position)
        if not item:
            return

        file_path = item.data(Qt.ItemDataRole.UserRole)
        is_dir = os.path.isdir(file_path)

        context_menu = QMenu(self)

        if is_dir:
            open_action = context_menu.addAction('📂 Открыть')
            open_action.triggered.connect(
                lambda: self.load_directory(file_path))

            context_menu.addSeparator()
        else:
            open_default_action = context_menu.addAction(
                '🚀 Открыть (системное приложение)')
            open_default_action.triggered.connect(
                lambda: self.open_file_with_default_app(file_path))

            view_action = context_menu.addAction('👁️ Просмотр (F3)')
            view_action.triggered.connect(
                lambda: self.view_file_in_viewer(file_path))

            edit_action = context_menu.addAction('✏️ Редактировать (F4)')
            edit_action.triggered.connect(
                lambda: self.edit_file_in_editor(file_path))

            context_menu.addSeparator()

        copy_action = context_menu.addAction('📋 Копировать (F5)')
        copy_action.triggered.connect(
            lambda: self.copy_to_other_panel(file_path))

        move_action = context_menu.addAction('➡️ Переместить (F6)')
        move_action.triggered.connect(
            lambda: self.move_to_other_panel(file_path))

        context_menu.addSeparator()

        rename_action = context_menu.addAction('✎ Переименовать (F2)')
        rename_action.triggered.connect(lambda: self.rename_item(file_path))

        delete_action = context_menu.addAction('🗑️ Удалить (F8)')
        delete_action.triggered.connect(lambda: self.delete_item(file_path))

        context_menu.exec(self.file_list.mapToGlobal(position))

    def view_file_in_viewer(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()

        image_extensions = {'.jpg', '.jpeg', '.png',
                            '.gif', '.bmp', '.webp', '.ico', '.svg'}
        text_extensions = {'.txt', '.py', '.js', '.html', '.css', '.json', '.xml', '.md',
                           '.csv', '.log', '.ini', '.cfg', '.conf', '.yaml', '.yml', '.sh',
                           '.bat', '.c', '.cpp', '.h', '.java', '.php', '.rb', '.go', '.rs',
                           '.sql', '.r', '.m', '.swift', '.kt', '.ts', '.jsx', '.tsx', '.vue'}

        if ext in image_extensions:
            viewer = ImageViewer(file_path, self)
            viewer.exec()
        elif ext in text_extensions or ext == '':
            viewer = FileViewer(file_path, self, edit_mode=False)
            viewer.exec()
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))

    def edit_file_in_editor(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()

        text_extensions = {'.txt', '.py', '.js', '.html', '.css', '.json', '.xml', '.md',
                           '.csv', '.log', '.ini', '.cfg', '.conf', '.yaml', '.yml', '.sh',
                           '.bat', '.c', '.cpp', '.h', '.java', '.php', '.rb', '.go', '.rs',
                           '.sql', '.r', '.m', '.swift', '.kt', '.ts', '.jsx', '.tsx', '.vue'}

        if ext in text_extensions or ext == '':
            editor = FileViewer(file_path, self, edit_mode=True)
            editor.exec()
            self.refresh()
        else:
            QMessageBox.information(
                self, 'Информация',
                'Редактирование доступно только для текстовых файлов.\n'
                'Поддерживаемые форматы: .txt, .py, .js, .html, .css, .json, .xml, .md и другие.'
            )

    def copy_to_other_panel(self, source_path):
        if not self.parent_window:
            return

        if self.parent_window.active_panel == self:
            target_panel = self.parent_window.get_inactive_panel()
        else:
            target_panel = self.parent_window.active_panel

        target_dir = target_panel.current_path
        source_name = os.path.basename(source_path)
        target_path = os.path.join(target_dir, source_name)

        reply = QMessageBox.question(
            self, 'Копирование',
            f'Копировать:\n{source_path}\n\nВ:\n{target_path}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                if os.path.isdir(source_path):
                    shutil.copytree(source_path, target_path,
                                    dirs_exist_ok=True)
                else:
                    shutil.copy2(source_path, target_path)
                QMessageBox.information(self, 'Успех', 'Копирование завершено')
                if self.parent_window:
                    self.parent_window.refresh_panels()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось скопировать: {str(e)}')

    def move_to_other_panel(self, source_path):
        if not self.parent_window:
            return

        if self.parent_window.active_panel == self:
            target_panel = self.parent_window.get_inactive_panel()
        else:
            target_panel = self.parent_window.active_panel

        target_dir = target_panel.current_path
        source_name = os.path.basename(source_path)
        target_path = os.path.join(target_dir, source_name)

        reply = QMessageBox.question(
            self, 'Перемещение',
            f'Переместить:\n{source_path}\n\nВ:\n{target_path}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                shutil.move(source_path, target_path)
                QMessageBox.information(self, 'Успех', 'Перемещение завершено')
                if self.parent_window:
                    self.parent_window.refresh_panels()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось переместить: {str(e)}')

    def rename_item(self, file_path):
        old_name = os.path.basename(file_path)
        new_name, ok = QInputDialog.getText(
            self, 'Переименовать',
            'Введите новое имя:',
            text=old_name
        )

        if ok and new_name and new_name != old_name:
            new_path = os.path.join(os.path.dirname(file_path), new_name)
            try:
                os.rename(file_path, new_path)
                QMessageBox.information(
                    self, 'Успех', f'Переименовано в: {new_name}')
                self.refresh()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось переименовать: {str(e)}')

    def delete_item(self, file_path):
        reply = QMessageBox.question(
            self, 'Удаление',
            f'Удалить:\n{file_path}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                if os.path.isdir(file_path):
                    shutil.rmtree(file_path)
                else:
                    os.remove(file_path)
                QMessageBox.information(self, 'Успех', 'Удаление завершено')
                self.refresh()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось удалить: {str(e)}')

    def start_drag(self, supported_actions):
        """Начало перетаскивания файла/папки"""
        current_item = self.file_list.currentItem()
        if not current_item:
            return

        # Получаем полный путь из UserRole
        file_path = current_item.data(Qt.ItemDataRole.UserRole)
        # Не даём перетаскивать элемент ".." (родительская папка)
        if not file_path or file_path == os.path.dirname(self.current_path):
            return

        # Создаём объект перетаскивания
        drag = QDrag(self.file_list)
        mime_data = QMimeData()
        # Упаковываем путь в двух форматах для совместимости
        mime_data.setUrls([QUrl.fromLocalFile(file_path)])  # URL формат
        mime_data.setText(file_path)  # Текстовый формат
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.MoveAction)  # Запускаем операцию перемещения

    def drag_enter_event(self, event):
        """Обработка входа перетаскиваемого объекта в зону виджета"""
        # Принимаем только файлы (URL или текст с путём)
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def drag_move_event(self, event):
        """Обработка движения перетаскиваемого объекта над виджетом"""
        # Проверяем формат данных при каждом движении
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def drop_event(self, event):
        """Обработка сброса файла на виджет"""
        # Приоритет: сначала пробуем извлечь URL
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                source_path = urls[0].toLocalFile()  # Конвертируем URL в путь
                self.handle_file_drop(source_path)
                event.acceptProposedAction()
        # Запасной вариант: текстовое представление пути
        elif event.mimeData().hasText():
            source_path = event.mimeData().text()
            self.handle_file_drop(source_path)
            event.acceptProposedAction()
        else:
            event.ignore()

    def handle_file_drop(self, source_path):
        if not os.path.exists(source_path):
            return

        target_dir = self.current_path
        source_name = os.path.basename(source_path)
        target_path = os.path.join(target_dir, source_name)

        if os.path.dirname(source_path) == target_dir:
            return

        reply = QMessageBox.question(
            self, 'Перемещение',
            f'Переместить:\n{source_path}\n\nВ:\n{target_path}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                shutil.move(source_path, target_path)
                QMessageBox.information(self, 'Успех', 'Перемещение завершено')
                self.refresh()
                if self.parent_window:
                    self.parent_window.refresh_panels()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось переместить: {str(e)}')


# ============================================================================
# Главное окно приложения
# ============================================================================

class DualExplorerWindow(QMainWindow):
    """
    Главное окно с двумя панелями файлов

    Центральный компонент приложения. Координирует работу двух FilePanel,
    управляет горячими клавишами, темами и AI-ассистентом.

    Ключевые особенности:
    - Две независимые панели (left_panel, right_panel)
    - Отслеживание активной панели через self.active_panel
    - Все операции (F2-F8) применяются к активной панели
    - Переключение панелей кликом мыши
    - Поддержка светлой и тёмной темы
    """

    def __init__(self, left_path=None, right_path=None):
        super().__init__()
        # Ссылка на текущую активную панель (левую или правую)
        self.active_panel = None
        # Начальные пути для панелей (переданные из StartupDialog)
        self.initial_left_path = left_path
        self.initial_right_path = right_path
        # Флаг тёмной темы (False = светлая, True = тёмная)
        self.dark_theme = False
        # Создаём интерфейс, горячие клавиши и применяем тему
        self.init_ui()
        self.setup_shortcuts()
        self.apply_theme()

    def init_ui(self):
        self.setWindowTitle('DualExplorer')
        self.setGeometry(100, 100, 1200, 700)

        self.create_menu()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout()
        central_widget.setLayout(main_layout)

        panels_layout = QHBoxLayout()

        self.left_panel = FilePanel(
            start_path=self.initial_left_path, parent_window=self)
        self.left_panel.file_list.currentItemChanged.connect(
            lambda: self.set_active_panel(self.left_panel))
        panels_layout.addWidget(self.left_panel)

        self.right_panel = FilePanel(
            start_path=self.initial_right_path, parent_window=self)
        self.right_panel.file_list.currentItemChanged.connect(
            lambda: self.set_active_panel(self.right_panel))
        panels_layout.addWidget(self.right_panel)

        main_layout.addLayout(panels_layout)

        ai_button_layout = QHBoxLayout()
        ai_button_layout.addStretch()

        self.ai_button = QPushButton('🤖 AI Ассистент')
        self.ai_button.setStyleSheet('''
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 15px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #005a9e;
            }
            QPushButton:pressed {
                background-color: #004578;
            }
        ''')
        self.ai_button.clicked.connect(self.open_ai_assistant)
        ai_button_layout.addWidget(self.ai_button)

        main_layout.addLayout(ai_button_layout)

        self.active_panel = None
        self.set_active_panel(self.left_panel)
        self.left_panel.file_list.setFocus()

    def create_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu('Файл')

        view_action = file_menu.addAction('Просмотр (F3)')
        view_action.setShortcut('F3')
        view_action.triggered.connect(self.view_file)

        edit_file_action = file_menu.addAction('Редактировать (F4)')
        edit_file_action.setShortcut('F4')
        edit_file_action.triggered.connect(self.edit_file)

        file_menu.addSeparator()

        copy_action = file_menu.addAction('Копировать (F5)')
        copy_action.setShortcut('F5')
        copy_action.triggered.connect(self.copy_file)

        move_action = file_menu.addAction('Переместить (F6)')
        move_action.setShortcut('F6')
        move_action.triggered.connect(self.move_file)

        file_menu.addSeparator()

        delete_action = file_menu.addAction('Удалить (F8)')
        delete_action.setShortcut('F8')
        delete_action.triggered.connect(self.delete_file)

        rename_action = file_menu.addAction('Переименовать (F2)')
        rename_action.setShortcut('F2')
        rename_action.triggered.connect(self.rename_file)

        file_menu.addSeparator()

        exit_action = file_menu.addAction('Выход')
        exit_action.setShortcut('Alt+F4')
        exit_action.triggered.connect(self.close)

        edit_menu = menubar.addMenu('Правка')

        mkdir_action = edit_menu.addAction('Создать папку (F7)')
        mkdir_action.setShortcut('F7')
        mkdir_action.triggered.connect(self.create_directory)

        mkfile_action = edit_menu.addAction('Создать файл (Shift+F7)')
        mkfile_action.setShortcut('Shift+F7')
        mkfile_action.triggered.connect(self.create_file)

        edit_menu.addSeparator()

        refresh_action = edit_menu.addAction('Обновить')
        refresh_action.setShortcut('Ctrl+R')
        refresh_action.triggered.connect(self.refresh_panels)

        view_menu = menubar.addMenu('Вид')

        change_left_path = view_menu.addAction('Изменить путь левой панели')
        change_left_path.triggered.connect(self.change_left_panel_path)

        change_right_path = view_menu.addAction('Изменить путь правой панели')
        change_right_path.triggered.connect(self.change_right_panel_path)

        view_menu.addSeparator()

        self.theme_action = view_menu.addAction('🌙 Темная тема')
        self.theme_action.setCheckable(True)
        self.theme_action.triggered.connect(self.toggle_theme)

    def setup_shortcuts(self):
        """Настройка горячих клавиш"""
        QShortcut(QKeySequence(Qt.Key.Key_F2),
                  self).activated.connect(self.rename_file)
        QShortcut(QKeySequence(Qt.Key.Key_F3),
                  self).activated.connect(self.view_file)
        QShortcut(QKeySequence(Qt.Key.Key_F4),
                  self).activated.connect(self.edit_file)
        QShortcut(QKeySequence(Qt.Key.Key_F5),
                  self).activated.connect(self.copy_file)
        QShortcut(QKeySequence(Qt.Key.Key_F6),
                  self).activated.connect(self.move_file)
        QShortcut(QKeySequence(Qt.Key.Key_F7),
                  self).activated.connect(self.create_directory)
        QShortcut(QKeySequence(Qt.Modifier.SHIFT | Qt.Key.Key_F7),
                  self).activated.connect(self.create_file)
        QShortcut(QKeySequence(Qt.Key.Key_F8),
                  self).activated.connect(self.delete_file)

    def set_active_panel(self, panel):
        if self.active_panel:
            self.active_panel.set_active(False)
        self.active_panel = panel
        self.active_panel.set_active(True)

    def get_inactive_panel(self):
        if self.active_panel == self.left_panel:
            return self.right_panel
        return self.left_panel

    def view_file(self):
        if not self.active_panel:
            return

        selected_path = self.active_panel.get_selected_path()
        if not selected_path or os.path.isdir(selected_path):
            QMessageBox.information(
                self, 'Информация', 'Выберите файл для просмотра')
            return

        ext = os.path.splitext(selected_path)[1].lower()

        image_extensions = {'.jpg', '.jpeg', '.png',
                            '.gif', '.bmp', '.webp', '.ico', '.svg'}
        text_extensions = {'.txt', '.py', '.js', '.html', '.css', '.json', '.xml', '.md',
                           '.csv', '.log', '.ini', '.cfg', '.conf', '.yaml', '.yml', '.sh',
                           '.bat', '.c', '.cpp', '.h', '.java', '.php', '.rb', '.go', '.rs',
                           '.sql', '.r', '.m', '.swift', '.kt', '.ts', '.jsx', '.tsx', '.vue'}

        if ext in image_extensions:
            viewer = ImageViewer(selected_path, self)
            viewer.exec()
        elif ext in text_extensions or ext == '':
            viewer = FileViewer(selected_path, self, edit_mode=False)
            viewer.exec()
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(selected_path))

    def edit_file(self):
        if not self.active_panel:
            return

        selected_path = self.active_panel.get_selected_path()
        if not selected_path or os.path.isdir(selected_path):
            QMessageBox.information(
                self, 'Информация', 'Выберите файл для редактирования')
            return

        ext = os.path.splitext(selected_path)[1].lower()

        text_extensions = {'.txt', '.py', '.js', '.html', '.css', '.json', '.xml', '.md',
                           '.csv', '.log', '.ini', '.cfg', '.conf', '.yaml', '.yml', '.sh',
                           '.bat', '.c', '.cpp', '.h', '.java', '.php', '.rb', '.go', '.rs',
                           '.sql', '.r', '.m', '.swift', '.kt', '.ts', '.jsx', '.tsx', '.vue'}

        if ext in text_extensions or ext == '':
            editor = FileViewer(selected_path, self, edit_mode=True)
            editor.exec()
            self.active_panel.refresh()
        else:
            QMessageBox.information(
                self, 'Информация',
                'Редактирование доступно только для текстовых файлов.\n'
                'Поддерживаемые форматы: .txt, .py, .js, .html, .css, .json, .xml, .md и другие.'
            )

    def copy_file(self):
        if not self.active_panel:
            return

        source_path = self.active_panel.get_selected_path()
        if not source_path:
            return

        target_panel = self.get_inactive_panel()
        target_dir = target_panel.current_path

        source_name = os.path.basename(source_path)
        target_path = os.path.join(target_dir, source_name)

        reply = QMessageBox.question(
            self, 'Копирование',
            f'Копировать:\n{source_path}\n\nВ:\n{target_path}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                if os.path.isdir(source_path):
                    shutil.copytree(source_path, target_path,
                                    dirs_exist_ok=True)
                else:
                    shutil.copy2(source_path, target_path)
                QMessageBox.information(self, 'Успех', 'Копирование завершено')
                self.refresh_panels()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось скопировать: {str(e)}')

    def move_file(self):
        if not self.active_panel:
            return

        source_path = self.active_panel.get_selected_path()
        if not source_path:
            return

        target_panel = self.get_inactive_panel()
        target_dir = target_panel.current_path

        source_name = os.path.basename(source_path)
        target_path = os.path.join(target_dir, source_name)

        reply = QMessageBox.question(
            self, 'Перемещение',
            f'Переместить:\n{source_path}\n\nВ:\n{target_path}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                shutil.move(source_path, target_path)
                QMessageBox.information(self, 'Успех', 'Перемещение завершено')
                self.refresh_panels()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось переместить: {str(e)}')

    def create_directory(self):
        """Создание новой папки (F7)"""
        if not self.active_panel:
            return

        dir_name, ok = QInputDialog.getText(
            self, 'Создать папку', 'Введите имя новой папки:')

        if ok and dir_name:
            new_dir_path = os.path.join(
                self.active_panel.current_path, dir_name)
            try:
                os.makedirs(new_dir_path)
                QMessageBox.information(
                    self, 'Успех', f'Папка создана: {dir_name}')
                self.active_panel.refresh()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось создать папку: {str(e)}')

    def create_file(self):
        """Создание нового файла (Shift+F7)"""
        if not self.active_panel:
            return

        file_name, ok = QInputDialog.getText(
            self, 'Создать файл', 'Введите имя нового файла:')

        if ok and file_name:
            new_file_path = os.path.join(
                self.active_panel.current_path, file_name)
            try:
                with open(new_file_path, 'w', encoding='utf-8') as f:
                    f.write('')
                QMessageBox.information(
                    self, 'Успех', f'Файл создан: {file_name}')
                self.active_panel.refresh()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось создать файл: {str(e)}')

    def delete_file(self):
        if not self.active_panel:
            return

        selected_path = self.active_panel.get_selected_path()
        if not selected_path:
            return

        reply = QMessageBox.question(
            self, 'Удаление',
            f'Удалить:\n{selected_path}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                if os.path.isdir(selected_path):
                    shutil.rmtree(selected_path)
                else:
                    os.remove(selected_path)
                QMessageBox.information(self, 'Успех', 'Удаление завершено')
                self.active_panel.refresh()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось удалить: {str(e)}')

    def rename_file(self):
        if not self.active_panel:
            return

        selected_path = self.active_panel.get_selected_path()
        if not selected_path:
            return

        old_name = os.path.basename(selected_path)
        new_name, ok = QInputDialog.getText(
            self, 'Переименовать',
            'Введите новое имя:',
            text=old_name
        )

        if ok and new_name and new_name != old_name:
            new_path = os.path.join(os.path.dirname(selected_path), new_name)
            try:
                os.rename(selected_path, new_path)
                QMessageBox.information(
                    self, 'Успех', f'Переименовано в: {new_name}')
                self.active_panel.refresh()
            except Exception as e:
                QMessageBox.critical(
                    self, 'Ошибка', f'Не удалось переименовать: {str(e)}')

    def refresh_panels(self):
        self.left_panel.refresh()
        self.right_panel.refresh()

    def change_left_panel_path(self):
        directory = QFileDialog.getExistingDirectory(
            self, 'Выберите директорию для левой панели',
            self.left_panel.current_path
        )
        if directory:
            self.left_panel.load_directory(directory)

    def change_right_panel_path(self):
        directory = QFileDialog.getExistingDirectory(
            self, 'Выберите директорию для правой панели',
            self.right_panel.current_path
        )
        if directory:
            self.right_panel.load_directory(directory)

    def toggle_theme(self):
        self.dark_theme = not self.dark_theme
        self.theme_action.setText(
            '☀️ Светлая тема' if self.dark_theme else '🌙 Темная тема')
        self.apply_theme()

    def apply_theme(self):
        if self.dark_theme:
            dark_style = """
                QMainWindow {
                    background-color: #1e1e1e;
                }
                QWidget {
                    background-color: #1e1e1e;
                    color: #d4d4d4;
                }
                QLineEdit {
                    background-color: #2d2d2d;
                    border: 1px solid #3e3e3e;
                    border-radius: 3px;
                    padding: 5px;
                    color: #d4d4d4;
                    selection-background-color: #264f78;
                }
                QLineEdit:focus {
                    border: 1px solid #007acc;
                }
                QPushButton {
                    background-color: #0e639c;
                    color: white;
                    border: none;
                    border-radius: 3px;
                    padding: 5px 10px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #1177bb;
                }
                QPushButton:pressed {
                    background-color: #0d5a8f;
                }
                QListWidget {
                    background-color: #252526;
                    border: 1px solid #3e3e3e;
                    border-radius: 3px;
                    color: #d4d4d4;
                    outline: none;
                }
                QListWidget::item {
                    padding: 5px;
                    border-radius: 2px;
                }
                QListWidget::item:selected {
                    background-color: #094771;
                }
                QListWidget::item:hover {
                    background-color: #2a2d2e;
                }
                QComboBox {
                    background-color: #2d2d2d;
                    border: 1px solid #3e3e3e;
                    border-radius: 3px;
                    padding: 5px;
                    color: #d4d4d4;
                }
                QComboBox:hover {
                    border: 1px solid #007acc;
                }
                QComboBox::drop-down {
                    border: none;
                }
                QComboBox QAbstractItemView {
                    background-color: #2d2d2d;
                    border: 1px solid #3e3e3e;
                    selection-background-color: #094771;
                    color: #d4d4d4;
                }
                QLabel {
                    color: #d4d4d4;
                }
                QMenuBar {
                    background-color: #2d2d2d;
                    color: #d4d4d4;
                }
                QMenuBar::item:selected {
                    background-color: #094771;
                }
                QMenu {
                    background-color: #2d2d2d;
                    border: 1px solid #3e3e3e;
                    color: #d4d4d4;
                }
                QMenu::item:selected {
                    background-color: #094771;
                }
                QTextEdit {
                    background-color: #1e1e1e;
                    border: 1px solid #3e3e3e;
                    color: #d4d4d4;
                }
            """
            self.setStyleSheet(dark_style)
        else:
            light_style = """
                QMainWindow {
                    background-color: #f3f3f3;
                }
                QWidget {
                    background-color: #f3f3f3;
                    color: #000000;
                }
                QLineEdit {
                    background-color: white;
                    border: 1px solid #cccccc;
                    border-radius: 3px;
                    padding: 5px;
                    color: #000000;
                }
                QLineEdit:focus {
                    border: 1px solid #0078d4;
                }
                QPushButton {
                    background-color: #0078d4;
                    color: white;
                    border: none;
                    border-radius: 3px;
                    padding: 5px 10px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #106ebe;
                }
                QPushButton:pressed {
                    background-color: #005a9e;
                }
                QListWidget {
                    background-color: white;
                    border: 1px solid #cccccc;
                    border-radius: 3px;
                    color: #000000;
                    outline: none;
                }
                QListWidget::item {
                    padding: 5px;
                    border-radius: 2px;
                }
                QListWidget::item:selected {
                    background-color: #cce8ff;
                    color: #000000;
                }
                QListWidget::item:hover {
                    background-color: #e5f3ff;
                }
                QComboBox {
                    background-color: white;
                    border: 1px solid #cccccc;
                    border-radius: 3px;
                    padding: 5px;
                    color: #000000;
                }
                QComboBox:hover {
                    border: 1px solid #0078d4;
                }
                QComboBox QAbstractItemView {
                    background-color: white;
                    border: 1px solid #cccccc;
                    selection-background-color: #cce8ff;
                    color: #000000;
                }
                QLabel {
                    color: #000000;
                }
                QMenuBar {
                    background-color: #f3f3f3;
                    color: #000000;
                }
                QMenuBar::item:selected {
                    background-color: #cce8ff;
                }
                QMenu {
                    background-color: white;
                    border: 1px solid #cccccc;
                    color: #000000;
                }
                QMenu::item:selected {
                    background-color: #cce8ff;
                }
                QTextEdit {
                    background-color: white;
                    border: 1px solid #cccccc;
                    color: #000000;
                }
            """
            self.setStyleSheet(light_style)

        if self.active_panel:
            self.active_panel.set_active(True)
            inactive = self.get_inactive_panel()
            if inactive:
                inactive.set_active(False)

    def open_ai_assistant(self):
        api_key = 'sk-2c771c4bc91c4e1cac5e109887172fce'
        dialog = AIDialog(self, api_key)
        dialog.show()


# ============================================================================
# База данных для хранения истории AI-чата
# ============================================================================

class ChatDatabase:
    """
    Управление SQLite базой данных с историей общения с AI

    Хранит полную историю диалогов с AI-ассистентом, включая:
    - Сообщения пользователя и ответы AI
    - Выполненные действия (copy, move, delete, create_folder, create_file, rename)
    - Параметры действий в формате JSON
    - Статус выполнения (pending, success, error)
    - Сообщения об ошибках

    Используется для:
    1. Формирования контекста (последние 6 диалогов) для AI
    2. Отображения истории в интерфейсе AI-ассистента
    3. Отладки и анализа работы AI
    """

    def __init__(self, db_path='chat_history.db'):
        """Инициализация БД (создание файла и таблицы если их нет)"""
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """
        Создание таблицы chat_history если её нет

        Структура таблицы:
        - id: уникальный идентификатор записи
        - timestamp: время создания записи (ISO формат)
        - user_message: текст запроса пользователя
        - ai_response: ответ AI (текстовое сообщение)
        - action: тип действия (copy/move/delete/create_folder/create_file/rename)
        - params: JSON с параметрами действия (pattern, from, to, name, content)
        - status: статус выполнения (pending/success/error)
        - error_message: текст ошибки если status=error
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_message TEXT NOT NULL,
                ai_response TEXT,
                action TEXT,
                params TEXT,
                status TEXT,
                error_message TEXT
            )
        ''')
        conn.commit()
        conn.close()

    def add_message(self, user_message, ai_response=None, action=None, params=None, status='pending', error_message=None):
        """
        Добавление новой записи в историю

        Вызывается дважды в жизненном цикле запроса:
        1. При отправке запроса (status='pending', ai_response=None)
        2. После получения ответа (через update_message)

        Args:
            user_message: текст запроса пользователя
            ai_response: ответ AI (обычно None при создании)
            action: тип действия (copy/move/delete и т.д.)
            params: словарь с параметрами (будет сериализован в JSON)
            status: 'pending', 'success' или 'error'
            error_message: текст ошибки если есть

        Returns:
            message_id: ID созданной записи (для последующего update)
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        timestamp = datetime.now().isoformat()
        # Сериализуем параметры в JSON (или None если их нет)
        params_json = json.dumps(params) if params else None

        # Используем запрос с параметрами
        cursor.execute('''
            INSERT INTO chat_history (timestamp, user_message, ai_response, action, params, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (timestamp, user_message, ai_response, action, params_json, status, error_message))

        conn.commit()
        message_id = cursor.lastrowid  # ID только что созданной записи
        conn.close()
        return message_id

    def update_message(self, message_id, ai_response=None, action=None, params=None, status=None, error_message=None):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        updates = []
        values = []

        if ai_response is not None:
            updates.append('ai_response = ?')
            values.append(ai_response)
        if action is not None:
            updates.append('action = ?')
            values.append(action)
        if params is not None:
            updates.append('params = ?')
            values.append(json.dumps(params))
        if status is not None:
            updates.append('status = ?')
            values.append(status)
        if error_message is not None:
            updates.append('error_message = ?')
            values.append(error_message)

        if updates:
            values.append(message_id)
            query = f"UPDATE chat_history SET {', '.join(updates)} WHERE id = ?"
            cursor.execute(query, values)
            conn.commit()

        conn.close()

    def get_recent_history(self, count):
        """Получение последних N записей в хронологическом порядке"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        # ORDER BY id DESC - сначала получаем новые записи
        cursor.execute('''
            SELECT id, timestamp, user_message, ai_response, action, params, status, error_message
            FROM chat_history
            ORDER BY id DESC
            LIMIT ?
        ''', (count,))
        rows = cursor.fetchall()
        conn.close()

        # Преобразуем строки БД в словари Python
        history = []
        for row in rows:
            history.append({
                'id': row[0],
                'timestamp': row[1],
                'user_message': row[2],
                'ai_response': row[3],
                'action': row[4],
                # Десериализация JSON
                'params': json.loads(row[5]) if row[5] else None,
                'status': row[6],
                'error_message': row[7]
            })
        # Разворачиваем список, чтобы старые записи были первыми (для контекста AI)
        return list(reversed(history))

    def clear_history(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM chat_history')
        conn.commit()
        conn.close()


# ============================================================================
# ИИ ассистент для простого взаимодействия
# ============================================================================

class AIDialog(QDialog):
    """
    Диалог AI-ассистента с поддержкой контекста (последние 7 запросов)

    Архитектура:
    - Использует threading.Thread для асинхронных запросов к API
    - pyqtSignal для безопасной передачи данных из потока в UI
    - ChatDatabase для хранения истории и формирования контекста
    - OpenAI SDK для работы с DeepSeek API

    Поток работы:
    1. Пользователь вводит запрос → send()
    2. Запрос сохраняется в БД (status='pending')
    3. В отдельном потоке вызывается API с контекстом (system + 6 последних диалогов)
    4. Ответ AI парсится как JSON: {"action": "...", "params": {...}, "message": "..."}
    5. Через pyqtSignal ответ передаётся в главный поток → on_response()
    6. execute() выполняет действие (copy/move/delete и т.д.)
    7. БД обновляется (status='success' или 'error')

    Поддерживаемые действия:
    - copy: копирование файлов по паттерну
    - move: перемещение файлов
    - delete: удаление файлов/папок
    - create_folder: создание папки
    - create_file: создание файла с содержимым
    - rename: переименование файла/папки
    """
    # Сигнал для межпоточного взаимодействия (ответ AI, успех/ошибка)
    response_received = pyqtSignal(str, bool)

    def __init__(self, main_window, api_key):
        super().__init__(main_window)
        # Ссылка на главное окно (для доступа к панелям)
        self.main_window = main_window
        # API ключ DeepSeek
        self.api_key = api_key
        # Список сообщений для отображения в интерфейсе
        self.messages = []
        # База данных для хранения истории
        self.db = ChatDatabase()
        # ID текущего сообщения (для обновления после получения ответа)
        self.current_message_id = None
        # Подключаем сигнал к обработчику (для обновления UI из потока)
        self.response_received.connect(self.on_response)
        self.init_ui()
        # Загружаем историю из БД при открытии
        self.load_history()

    def init_ui(self):
        self.setWindowTitle('🤖 AI Ассистент')
        self.setGeometry(200, 200, 600, 550)

        layout = QVBoxLayout()
        self.setLayout(layout)

        info = QLabel('Напиши что хочешь сделать с файлами')
        info.setStyleSheet('padding: 10px; color: #666; font-weight: bold;')
        layout.addWidget(info)

        help_text = QLabel(
            '<b>Доступные команды:</b><br>'
            '• <b>Копировать:</b> "скопируй все .txt файлы в правую панель"<br>'
            '• <b>Переместить:</b> "перемести все .jpg в левую панель"<br>'
            '• <b>Удалить:</b> "удали все .tmp файлы"<br>'
            '• <b>Создать папку:</b> "создай папку Documents"<br>'
            '• <b>Создать файл:</b> "создай файл readme.txt с текстом Hello"<br>'
            '• <b>Переименовать:</b> "переименуй test.txt в final.txt"<br>'
            '<br><i>💾 Сохраняются последние 7 запросов с контекстом</i>'
        )
        help_text.setStyleSheet(
            'padding: 10px; '
            'background-color: #f0f8ff; '
            'border: 1px solid #b0d4f1; '
            'border-radius: 5px; '
            'color: #333; '
            'font-size: 11pt;'
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        self.chat = QTextEdit()
        self.chat.setReadOnly(True)
        layout.addWidget(self.chat)

        input_layout = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            'Например: скопируй все .txt файлы в правую панель')
        self.input.returnPressed.connect(self.send)
        input_layout.addWidget(self.input)

        btn = QPushButton('Отправить')
        btn.clicked.connect(self.send)
        input_layout.addWidget(btn)

        layout.addLayout(input_layout)

    def load_history(self):
        history = self.db.get_recent_history(7)
        for entry in history:
            self.chat.append(f'<b>Вы:</b> {entry["user_message"]}<br>')
            if entry['ai_response']:
                self.chat.append(f'<b>AI:</b> {entry["ai_response"]}<br>')
            if entry['status'] == 'success':
                self.chat.append('<b>✅ Готово!</b><br>')
            elif entry['status'] == 'error' and entry['error_message']:
                self.chat.append(
                    f'<b>❌ Ошибка:</b> {entry["error_message"]}<br>')

    def send(self):
        """
        Отправка запроса к AI с контекстом последних 6 диалогов

        Это ключевой метод AI-ассистента. Алгоритм работы:

        1. Валидация и сохранение запроса в БД
        2. Формирование system_prompt с информацией о панелях
        3. Получение последних 6 диалогов из БД для контекста
        4. Формирование массива messages: [system, user1, assistant1, ..., user6, assistant6, current_user]
        5. Запуск API-запроса в отдельном потоке (threading.Thread)
        6. Ожидание ответа через pyqtSignal → on_response()

        Контекст (7 запросов = 6 предыдущих + 1 текущий) позволяет AI:
        - Помнить о предыдущих операциях
        - Понимать относительные команды ("скопируй это в правую панель")
        - Вести осмысленный диалог
        """
        text = self.input.text().strip()
        if not text:
            return

        # Сохраняем сообщение в БД со статусом 'pending'
        self.current_message_id = self.db.add_message(text)

        self.input.clear()
        self.input.setEnabled(False)  # Блокируем ввод на время обработки
        self.chat.append(f'<b>Вы:</b> {text}<br>')
        self.chat.append(f'<i style="color: #999;">⏳ Думаю...</i><br>')

        # Получаем информацию о панелях для формирования контекста AI
        # AI должен знать, где какие файлы и какая панель активна
        left = self.main_window.left_panel.current_path
        right = self.main_window.right_panel.current_path
        active = 'левая' if self.main_window.active_panel == self.main_window.left_panel else 'правая'

        system_prompt = f"""Ты - ассистент файлового менеджера.
Левая панель: {left}
Правая панель: {right}
Активная панель: {active}

ВАЖНО: Все операции (copy, move, delete, rename) работают только с ОДНИМ файлом или ШАБЛОНОМ за раз!
- Если просят скопировать несколько конкретных файлов (file1.txt и file2.txt) - верни error с объяснением, что нужно делать по одному файлу.
- Используй шаблоны (*.txt, file?.doc) для групповых операций.
- Для конкретного файла указывай его полное имя как pattern.

Ответь ТОЛЬКО JSON в формате:
- copy: {{"action": "copy", "params": {{"pattern": "*.txt", "from": "left/right", "to": "left/right"}}, "message": "что делаю"}}
- move: {{"action": "move", "params": {{"pattern": "*.txt", "from": "left/right", "to": "left/right"}}, "message": "что делаю"}}
- delete: {{"action": "delete", "params": {{"pattern": "*.txt", "from": "left/right"}}, "message": "что делаю"}}
- create_folder: {{"action": "create_folder", "params": {{"name": "имя_папки"}}, "message": "что делаю"}}
- create_file: {{"action": "create_file", "params": {{"name": "имя_файла", "content": "текст"}}, "message": "что делаю"}}
- rename: {{"action": "rename", "params": {{"old_name": "старое_имя.txt", "new_name": "новое_имя.txt"}}, "message": "что делаю"}}

Если непонятно или просят несколько файлов - action: "error", message: "объяснение"
"""

        # Формируем сообщения для API
        messages = [{"role": "system", "content": system_prompt}]

        # Добавляем последние 6 диалогов для контекста (AI помнит предыдущие команды)
        history = self.db.get_recent_history(6)
        for entry in history:
            messages.append({"role": "user", "content": entry["user_message"]})
            if entry['ai_response']:
                messages.append(
                    {"role": "assistant", "content": entry["ai_response"]})

        # Добавляем текущий запрос
        messages.append({"role": "user", "content": text})

        import threading

        def call_ai():
            """Функция для выполнения в отдельном потоке"""
            try:
                # Создаём клиент для работы с DeepSeek API
                from openai import OpenAI
                client = OpenAI(api_key=self.api_key,
                                base_url="https://api.deepseek.com")
                # Отправляем запрос к AI (это может занять 2-10 секунд)
                response = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=messages,
                    temperature=0.3,
                    stream=False
                )
                answer = response.choices[0].message.content
                # Отправляем результат в главный поток через сигнал
                self.response_received.emit(answer, True)
            except Exception as e:
                print(e)  # Выводим ошибку в консоль для отладки
                self.response_received.emit(str(e), False)

        # Запускаем запрос к AI в отдельном потоке (daemon=True для автозавершения)
        threading.Thread(target=call_ai, daemon=True).start()

    def on_response(self, answer, success):
        self.input.setEnabled(True)

        html = self.chat.toHtml()
        html = html.replace('<i style="color: #999;">⏳ Думаю...</i><br>', '')
        self.chat.setHtml(html)

        if not success:
            self.chat.append(
                f'<b style="color: red;">Ошибка:</b> {answer}<br>')
            if self.current_message_id:
                self.db.update_message(
                    self.current_message_id, status='error', error_message=answer)
            return

        try:
            answer = answer.strip()
            if answer.startswith('```'):
                answer = answer.split('```')[1]
                if answer.startswith('json'):
                    answer = answer[4:]
            answer = answer.strip()

            cmd = json.loads(answer)
            ai_message = cmd.get("message", "")
            self.chat.append(f'<b>AI:</b> {ai_message}<br>')

            if self.current_message_id:
                self.db.update_message(
                    self.current_message_id,
                    ai_response=ai_message,
                    action=cmd.get('action'),
                    params=cmd.get('params')
                )

            if cmd['action'] == 'error':
                if self.current_message_id:
                    self.db.update_message(
                        self.current_message_id, status='error')
                return

            self.execute(cmd)

        except Exception as e:
            error_msg = str(e)
            self.chat.append(
                f'<b style="color: red;">Ошибка:</b> {error_msg}<br>')
            if self.current_message_id:
                self.db.update_message(
                    self.current_message_id, status='error', error_message=error_msg)

    def execute(self, cmd):
        """
        Выполнение команды AI

        Принимает распарсенный JSON от AI и вызывает соответствующий метод.
        Все методы (do_copy, do_move и т.д.) выбрасывают исключения при ошибках,
        которые перехватываются в on_response() и сохраняются в БД.

        Args:
            cmd: словарь с ключами 'action' и 'params'
                action: 'copy', 'move', 'delete', 'create_folder', 'create_file', 'rename'
                params: словарь с параметрами (зависит от action)

        Raises:
            ValueError: если операция невозможна (нет файлов, несколько файлов и т.д.)
            PermissionError: если нет прав доступа
            FileNotFoundError: если файл не найден
            Exception: другие ошибки файловой системы
        """
        action = cmd['action']
        params = cmd.get('params', {})

        # Логируем для отладки
        print(f"[AI] execute: action={action}, params={params}")

        try:
            # Диспетчеризация команд
            if action == 'copy':
                self.do_copy(params)
            elif action == 'move':
                self.do_move(params)
            elif action == 'delete':
                self.do_delete(params)
            elif action == 'create_folder':
                self.do_create_folder(params)
            elif action == 'create_file':
                self.do_create_file(params)
            elif action == 'rename':
                self.do_rename(params)

            self.main_window.refresh_panels()
            self.chat.append('<b>✅ Готово!</b><br>')

            if self.current_message_id:
                self.db.update_message(
                    self.current_message_id, status='success')
        except Exception as e:
            error_msg = str(e)
            self.chat.append(f'<b>❌ Ошибка:</b> {error_msg}<br>')
            if self.current_message_id:
                self.db.update_message(
                    self.current_message_id, status='error', error_message=error_msg)

    def do_copy(self, p):
        """Копирование файлов по шаблону (например, *.txt)"""
        # Определяем исходную и целевую панели
        from_panel = self.main_window.left_panel if p.get(
            'from') == 'left' else self.main_window.right_panel
        to_panel = self.main_window.right_panel if p.get(
            'to') == 'right' else self.main_window.left_panel
        pattern = p.get('pattern', '*')  # По умолчанию все файлы

        # Проверка на попытку скопировать несколько конкретных файлов через запятую
        if ',' in pattern or ' и ' in pattern.lower() or ' and ' in pattern.lower():
            raise ValueError(
                "Нельзя копировать несколько конкретных файлов за раз. Используйте шаблоны (*.txt) или делайте по одному файлу.")

        print(
            f"[AI] do_copy: pattern={pattern}, from={from_panel.current_path}, to={to_panel.current_path}")

        # Используем fnmatch для сопоставления имён файлов с шаблоном
        import fnmatch
        copied_count = 0
        for item in os.listdir(from_panel.current_path):
            # Проверяем, подходит ли файл под шаблон (например, *.txt)
            if fnmatch.fnmatch(item, pattern):
                src = os.path.join(from_panel.current_path, item)
                dst = os.path.join(to_panel.current_path, item)
                print(f"[AI] Copying: {item}")
                try:
                    if os.path.isdir(src):
                        # dirs_exist_ok=True позволяет копировать в существующую папку
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        # copy2 сохраняет метаданные (дату, права доступа)
                        shutil.copy2(src, dst)
                    copied_count += 1
                except Exception as e:
                    print(f"[AI] Error copying {item}: {e}")
                    raise Exception(f"Не удалось скопировать {item}: {e}")

        # Если ни один файл не подошёл под шаблон - выбрасываем ошибку
        if copied_count == 0:
            raise Exception(
                f"Не найдено файлов, соответствующих шаблону '{pattern}'")

    def do_move(self, p):
        from_panel = self.main_window.left_panel if p.get(
            'from') == 'left' else self.main_window.right_panel
        to_panel = self.main_window.right_panel if p.get(
            'to') == 'right' else self.main_window.left_panel
        pattern = p.get('pattern', '*')

        # Проверка на попытку переместить несколько файлов
        if ',' in pattern or ' и ' in pattern.lower() or ' and ' in pattern.lower():
            raise ValueError(
                "Нельзя перемещать несколько конкретных файлов за раз. Используйте шаблоны (*.txt) или делайте по одному файлу.")

        print(
            f"[AI] do_move: pattern={pattern}, from={from_panel.current_path}, to={to_panel.current_path}")

        import fnmatch
        moved_count = 0
        for item in os.listdir(from_panel.current_path):
            if fnmatch.fnmatch(item, pattern):
                src = os.path.join(from_panel.current_path, item)
                dst = os.path.join(to_panel.current_path, item)
                print(f"[AI] Moving: {item}")
                try:
                    shutil.move(src, dst)
                    moved_count += 1
                except Exception as e:
                    print(f"[AI] Error moving {item}: {e}")
                    raise Exception(f"Не удалось переместить {item}: {e}")

        if moved_count == 0:
            raise Exception(
                f"Не найдено файлов, соответствующих шаблону '{pattern}'")

    def do_delete(self, p):
        panel = self.main_window.left_panel if p.get(
            'from') == 'left' else self.main_window.right_panel
        pattern = p.get('pattern', '*')

        # Проверка на попытку удалить несколько файлов
        if ',' in pattern or ' и ' in pattern.lower() or ' and ' in pattern.lower():
            raise ValueError(
                "Нельзя удалять несколько конкретных файлов за раз. Используйте шаблоны (*.txt) или делайте по одному файлу.")

        print(f"[AI] do_delete: pattern={pattern}, from={panel.current_path}")

        import fnmatch
        deleted_count = 0
        for item in os.listdir(panel.current_path):
            if fnmatch.fnmatch(item, pattern):
                path = os.path.join(panel.current_path, item)
                print(f"[AI] Deleting: {item}")
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                    deleted_count += 1
                except Exception as e:
                    print(f"[AI] Error deleting {item}: {e}")
                    raise Exception(f"Не удалось удалить {item}: {e}")

        if deleted_count == 0:
            raise Exception(
                f"Не найдено файлов, соответствующих шаблону '{pattern}'")

    def do_create_folder(self, p):
        panel = self.main_window.active_panel
        name = p.get('name', 'Новая папка')
        path = os.path.join(panel.current_path, name)
        print(f"[AI] do_create_folder: name={name}, path={path}")
        os.makedirs(path, exist_ok=True)

    def do_create_file(self, p):
        panel = self.main_window.active_panel
        name = p.get('name', 'новый_файл.txt')
        content = p.get('content', '')
        path = os.path.join(panel.current_path, name)
        print(
            f"[AI] do_create_file: name={name}, path={path}, content_length={len(content)}")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

    def do_rename(self, p):
        panel = self.main_window.active_panel
        old_name = p.get('old_name') or p.get('old', '')
        new_name = p.get('new_name') or p.get('new', '')

        print(f"[AI] do_rename: old_name={old_name}, new_name={new_name}")

        if not old_name or not new_name:
            raise ValueError('Не указаны старое или новое имя файла')

        old_path = os.path.join(panel.current_path, old_name)
        new_path = os.path.join(panel.current_path, new_name)

        if not os.path.exists(old_path):
            raise FileNotFoundError(f'Файл "{old_name}" не найден')

        if os.path.exists(new_path):
            raise FileExistsError(f'Файл "{new_name}" уже существует')

        os.rename(old_path, new_path)


class StartupDialog(QDialog):
    """
    Диалог выбора начальных директорий при запуске приложения

    Отображается при старте программы и позволяет пользователю выбрать,
    какие директории будут открыты в левой и правой панелях.

    По умолчанию обе панели открывают домашнюю директорию пользователя.

    Использование:
    - Модальный диалог (блокирует запуск до выбора)
    - accept() → запуск с выбранными путями
    - reject() → закрытие приложения
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # Пути по умолчанию (домашняя директория)
        self.left_path = str(Path.home())
        self.right_path = str(Path.home())
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle('Выбор начальных директорий')
        self.setGeometry(300, 300, 500, 200)

        layout = QVBoxLayout()
        self.setLayout(layout)

        left_layout = QHBoxLayout()
        left_label = QLabel('Левая панель:')
        left_layout.addWidget(left_label)

        self.left_path_edit = QLabel(self.left_path)
        self.left_path_edit.setStyleSheet(
            'border: 1px solid gray; padding: 5px;')
        left_layout.addWidget(self.left_path_edit)

        left_btn = QPushButton('Обзор...')
        left_btn.clicked.connect(self.choose_left_path)
        left_layout.addWidget(left_btn)

        layout.addLayout(left_layout)

        right_layout = QHBoxLayout()
        right_label = QLabel('Правая панель:')
        right_layout.addWidget(right_label)

        self.right_path_edit = QLabel(self.right_path)
        self.right_path_edit.setStyleSheet(
            'border: 1px solid gray; padding: 5px;')
        right_layout.addWidget(self.right_path_edit)

        right_btn = QPushButton('Обзор...')
        right_btn.clicked.connect(self.choose_right_path)
        right_layout.addWidget(right_btn)

        layout.addLayout(right_layout)

        buttons_layout = QHBoxLayout()
        ok_btn = QPushButton('OK')
        ok_btn.clicked.connect(self.accept)
        buttons_layout.addWidget(ok_btn)

        cancel_btn = QPushButton('Отмена')
        cancel_btn.clicked.connect(self.reject)
        buttons_layout.addWidget(cancel_btn)

        layout.addLayout(buttons_layout)

    def choose_left_path(self):
        directory = QFileDialog.getExistingDirectory(
            self, 'Выберите директорию для левой панели',
            self.left_path
        )
        if directory:
            self.left_path = directory
            self.left_path_edit.setText(directory)

    def choose_right_path(self):
        directory = QFileDialog.getExistingDirectory(
            self, 'Выберите директорию для правой панели',
            self.right_path
        )
        if directory:
            self.right_path = directory
            self.right_path_edit.setText(directory)

    def get_paths(self):
        return self.left_path, self.right_path


def main():
    app = QApplication(sys.argv)

    startup_dialog = StartupDialog()
    if startup_dialog.exec() == QDialog.DialogCode.Accepted:
        left_path, right_path = startup_dialog.get_paths()
        window = DualExplorerWindow(left_path, right_path)
        window.show()
        sys.exit(app.exec())
    else:
        window = DualExplorerWindow()
        window.show()
        sys.exit(app.exec())


if __name__ == '__main__':
    main()
