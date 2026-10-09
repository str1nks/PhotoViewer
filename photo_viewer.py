"""
Простой просмотрщик фотографий в стиле Windows 11 (Fluent).

Установка:  pip install customtkinter pillow opencv-python sounddevice imageio-ffmpeg
Запуск:     python photo_viewer.py [путь_к_фото]

Управление:
  ← / →             предыдущее / следующее фото
  колесо мыши       плавное приближение / отдаление (к курсору)
  перетаскивание    перемещение (с инерцией) приближенного фото
  двойной клик      вписать в окно / приблизить
  + / -  и  0       приблизить / отдалить / вписать в окно
  R / Shift+R (L)   повернуть вправо / влево
  Ctrl+O            открыть фото / видео
  Видео (mp4, avi, mkv, mov, wmv, webm) воспроизводится сразу при открытии (со звуком):
  Пробел            пауза / продолжить (после конца - заново)
  Shift+← / Shift+→ перемотка на 5 секунд назад / вперёд
  ползунок          перемотка в любое место
  M                 выключить / включить звук
  ↑ / ↓             громкость
  F11               полный экран (Esc - выход)

Собранный PhotoViewer.exe при первом запуске сам предложит стать программой для фото и видео.
Вручную: PhotoViewer.exe --register  /  PhotoViewer.exe --unregister (полное удаление).
"""
import ctypes
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from tkinter import Canvas, Tk, filedialog, messagebox

import customtkinter as ctk
import cv2
import numpy as np
from PIL import Image, ImageOps, ImageTk

try:
    import sounddevice as sd
except Exception:   # нет библиотеки или звуковой карты - видео пойдёт без звука
    sd = None

if sys.platform == "win32":
    try:   # без этого sleep()/after() «щёлкают» шагом ~15 мс - отсюда рывки видео
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
sys.setswitchinterval(0.002)

try:   # своя иконка на панели задач, а не иконка python
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PhotoViewerPy.App")
except Exception:
    pass

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".ico"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".webm", ".m4v"}
EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

ctk.set_appearance_mode("system")  # следует за темой Windows
DARK = ctk.get_appearance_mode() == "Dark"

# Цвета Windows 11
BG = "#202020" if DARK else "#F3F3F3"
SUBTLE = "#9D9D9D" if DARK else "#6B6B6B"
SUBTLE_T = ("#6B6B6B", "#9D9D9D")
CARD = ("#FBFBFB", "#2C2C2C")
BORDER = ("#E5E5E5", "#3A3A3A")
HOVER = ("#E9E9E9", "#3A3A3A")
TEXT = ("#1A1A1A", "#FFFFFF")

ICONS = "Segoe Fluent Icons"
UI = "Segoe UI Variable Text"

MIN_SCALE, MAX_SCALE, STEP = 0.02, 32.0, 1.2
T = Image.Transpose
R = Image.Resampling

# Длительности анимаций (секунды)
ZOOM_T = 0.16
FIT_T = 0.26
FADE_T = 0.18
UI_HIDE_MS = 2200      # через сколько прячется панель в полноэкранном режиме
INERTIA_DECAY = 5.5    # чем больше, тем быстрее гаснет инерция


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def ease_out(p):
    return 1 - (1 - p) ** 3


def ease_in_out(p):
    return 4 * p ** 3 if p < 0.5 else 1 - (-2 * p + 2) ** 3 / 2


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def human_size(n):
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} КБ"
    return f"{n / 1024 / 1024:.1f} МБ"


def fmt_time(sec):
    m, s = divmod(int(max(0, sec)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ---------- установка в Windows (реестр, текущий пользователь, права админа не нужны) ----------
APP_ID = "PhotoViewerPy"
INSTALL_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                           "Programs", "PhotoViewer")
INSTALLED_EXE = os.path.join(INSTALL_DIR, "PhotoViewer.exe")


def _hkcu():
    import winreg
    return winreg, winreg.HKEY_CURRENT_USER


def _reg_set(path, name, value, kind=None):
    winreg, root = _hkcu()
    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, kind if kind is not None else winreg.REG_SZ, value)


def _reg_delete_tree(path):
    winreg, root = _hkcu()
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _reg_delete_tree(path + "\\" + sub)
        winreg.DeleteKey(root, path)
    except OSError:
        pass


def registered_exe():
    """Путь к exe, который сейчас прописан в Windows (или None)."""
    try:
        winreg, root = _hkcu()
        with winreg.OpenKey(root, rf"Software\Classes\{APP_ID}.File\shell\open\command") as k:
            return winreg.QueryValueEx(k, "")[0].split('"')[1]
    except Exception:
        return None


def register_app():
    """Копирует PhotoViewer.exe в папку программ и прописывает его в Windows
    (список «Приложения по умолчанию», «Открыть с помощью», «Установленные приложения»).
    Возвращает путь к установленному exe или None (если запущен не собранный .exe)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    winreg, root = _hkcu()
    src = os.path.abspath(sys.executable)
    exe = src
    try:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        if os.path.normcase(src) != os.path.normcase(INSTALLED_EXE):
            shutil.copy2(src, INSTALLED_EXE)
        exe = INSTALLED_EXE
    except OSError:       # например, копия сейчас запущена - тогда прописываем текущий файл
        pass

    cls = rf"Software\Classes\{APP_ID}.File"
    _reg_set(cls, "", "Фото и видео (PhotoViewer)")
    _reg_set(cls + r"\DefaultIcon", "", f'"{exe}",0')
    _reg_set(cls + r"\shell\open\command", "", f'"{exe}" "%1"')

    cap = rf"Software\{APP_ID}\Capabilities"
    _reg_set(cap, "ApplicationName", "PhotoViewer")
    _reg_set(cap, "ApplicationDescription", "Просмотр фото и видео")
    _reg_set(r"Software\RegisteredApplications", APP_ID, cap)
    for ext in sorted(EXTENSIONS):
        _reg_set(cap + r"\FileAssociations", ext, f"{APP_ID}.File")
        _reg_set(rf"Software\Classes\{ext}\OpenWithProgids", f"{APP_ID}.File", b"", winreg.REG_NONE)

    un = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
    _reg_set(un, "DisplayName", "PhotoViewer")
    _reg_set(un, "DisplayIcon", exe)
    _reg_set(un, "UninstallString", f'"{exe}" --unregister')
    _reg_set(un, "InstallLocation", os.path.dirname(exe))
    _reg_set(un, "NoModify", 1, winreg.REG_DWORD)
    _reg_set(un, "NoRepair", 1, winreg.REG_DWORD)
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)   # обновить ассоциации
    except Exception:
        pass
    return exe


def unregister_app():
    """Убирает все записи в реестре и (после выхода) папку с программой."""
    if sys.platform != "win32":
        return
    winreg, root = _hkcu()
    _reg_delete_tree(rf"Software\Classes\{APP_ID}.File")
    _reg_delete_tree(rf"Software\{APP_ID}")
    _reg_delete_tree(rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}")
    try:
        with winreg.OpenKey(root, r"Software\RegisteredApplications", 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_ID)
    except OSError:
        pass
    for ext in EXTENSIONS:
        try:
            with winreg.OpenKey(root, rf"Software\Classes\{ext}\OpenWithProgids", 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, f"{APP_ID}.File")
        except OSError:
            pass
    if getattr(sys, "frozen", False) and os.path.isdir(INSTALL_DIR):
        subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 4 >nul & rmdir /s /q "{INSTALL_DIR}"',
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:
        pass
# ---------- конец блока установки ----------


class AudioTrack:
    """Звук видео. ffmpeg достаёт дорожку во временный файл (в фоне), потом она
    играется через sounddevice. Видеопоток сам синхронизирует позицию звука."""

    SR = 44100
    volume = 0.8      # общие для всех видео
    muted = False

    def __init__(self, path):
        self.path = path
        self.ready = False        # дорожка извлечена и поток вывода запущен
        self.playing = False
        self.pos = 0              # позиция в сэмплах (кадрах PCM)
        self.latency = 0.0
        self.closed = False
        self._lock = threading.Lock()
        self._tmp = None
        self._file = None
        self._stream = None
        self._proc = None

    @staticmethod
    def _ffmpeg():
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return shutil.which("ffmpeg")

    def start(self):
        if sd is None:
            return
        threading.Thread(target=self._extract, daemon=True).start()

    def _extract(self):
        try:
            exe = self._ffmpeg()
            if not exe:
                return
            fd, self._tmp = tempfile.mkstemp(suffix=".pcm")
            os.close(fd)
            self._proc = subprocess.Popen(
                [exe, "-v", "error", "-nostdin", "-i", self.path, "-vn", "-sn",
                 "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "2", "-ar", str(self.SR),
                 "-y", self._tmp],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._proc.wait()
            if self.closed or os.path.getsize(self._tmp) < 4096:   # закрыли / в файле нет звука
                return
            self._file = open(self._tmp, "rb")
            self._stream = sd.OutputStream(samplerate=self.SR, channels=2, dtype="int16",
                                           blocksize=2048, latency="high",
                                           callback=self._callback)
            lat = self._stream.latency
            self.latency = float(lat[1] if isinstance(lat, (tuple, list)) else lat)
            self._stream.start()
            self.ready = True
        except Exception:
            self.ready = False
        finally:
            if not self.ready:
                self.close()

    def _callback(self, out, frames, _time, _status):
        if not self.playing or self._file is None:
            out.fill(0)
            return
        with self._lock:
            pos = self.pos
            try:
                self._file.seek(pos * 4)
                raw = self._file.read(frames * 4)
            except Exception:
                raw = b""
            got = len(raw) // 4
            self.pos = pos + got
        if got:
            chunk = np.frombuffer(raw, dtype=np.int16, count=got * 2).reshape(-1, 2)
            v = 0.0 if self.muted else self.volume ** 2
            if v < 0.999:
                chunk = (chunk * v).astype(np.int16)
            out[:got] = chunk
        if got < frames:
            out[got:] = 0

    def seek(self, sec):
        with self._lock:
            self.pos = max(0, int((sec + self.latency) * self.SR))

    def time(self):
        """Что сейчас слышно (сек), с учётом задержки вывода."""
        return self.pos / self.SR - self.latency

    def close(self):
        if self.closed and self._file is None and self._stream is None:
            return
        self.closed = True
        self.playing = False
        self.ready = False
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()
        except Exception:
            pass
        try:
            if self._stream is not None:
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        self._file = None
        if self._tmp:
            try:
                os.remove(self._tmp)
            except OSError:
                pass


class VideoPlayer(threading.Thread):
    """Декодирует видео в отдельном потоке (интерфейс не лагает).

    Поток сам держит темп воспроизведения, пропускает кадры, если не успевает,
    и заранее уменьшает кадр до размера окна - так в интерфейс попадает
    только маленькая готовая картинка. Интерфейс просто забирает self.frame.
    """

    def __init__(self, path, size):
        super().__init__(daemon=True)
        self.cap = cv2.VideoCapture(path)
        self.ok = self.cap.isOpened()
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if 1 <= fps <= 240 else 30.0
        n = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.total = n if n > 0 else 0          # 0 - длина неизвестна
        self.w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.size = size                        # (ширина, высота) окна - сюда вписываем кадр
        self.paused = False
        self.ended = False
        self.failed = False
        self.running = True
        self.seek_to = None                     # номер кадра, на который перемотать
        self.pos = 0                            # номер текущего кадра
        self.frame = None                       # (RGB-массив, номер кадра) - готов к показу
        self._raw = None                        # последний кадр в полном размере
        self._pub_size = None
        self.audio = AudioTrack(path) if self.ok else None
        self._reseek_t = 0.0

    def _sync_audio(self, force=False):
        a = self.audio
        if a is None or not a.ready:
            return
        t = self.pos / self.fps
        now = time.perf_counter()
        if force or not a.playing:
            a.seek(t)
            self._reseek_t = now
        elif abs(a.time() - t) > 0.5 and now - self._reseek_t > 2.0:   # редко и только при большом сбое
            a.seek(t)
            self._reseek_t = now
        a.playing = True

    def _publish(self, bgr, pos):
        self._raw = bgr
        cw, ch = self._pub_size = self.size
        h, w = bgr.shape[:2]
        k = min(cw / w, ch / h)
        small = cv2.resize(bgr, (max(2, int(w * k)), max(2, int(h * k))),
                           interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_LINEAR)
        self.frame = (cv2.cvtColor(small, cv2.COLOR_BGR2RGB), pos)

    def run(self):
        cap = self.cap
        try:
            if self.audio:
                self.audio.start()
            ok, f = cap.read()
            if not ok:
                self.failed = True
                return
            self._publish(f, 0)
            base, t0, idle = 0, time.perf_counter(), True
            while self.running:
                # окно изменили (например, при паузе) - перерисовываем текущий кадр
                if self._raw is not None and self.size != self._pub_size:
                    self._publish(self._raw, self.pos)

                n = self.seek_to
                if n is not None:                         # перемотка
                    self.seek_to = None
                    n = max(0, min(n, self.total - 1) if self.total else n)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, n)
                    ok, f = cap.read()
                    if ok:
                        self.pos, self.ended = n, False
                        self._publish(f, n)
                        self._sync_audio(True)
                        base, t0, idle = n, time.perf_counter(), False
                    continue

                if self.paused or self.ended:
                    if self.audio:
                        self.audio.playing = False
                    idle = True
                    time.sleep(0.01)
                    continue

                now = time.perf_counter()
                if idle:                                  # после паузы отсчёт времени заново
                    base, t0, idle = self.pos, now, False
                    self._sync_audio(True)
                due = t0 + (self.pos + 1 - base) / self.fps
                if now < due:
                    time.sleep(min(due - now, 0.01))
                    continue

                lag = int((now - due) * self.fps)
                if lag > 300:                             # зависли надолго - начинаем отсчёт заново
                    base, t0, lag = self.pos, now, 0
                lag = min(lag, 60)                        # иначе догоняем, пропуская кадры
                for _ in range(lag):                      # чуть отстали - пропускаем кадры
                    if not cap.grab():
                        break
                    self.pos += 1
                ok, f = cap.read()
                if not ok:
                    self.ended = True
                    continue
                self.pos += 1
                self._publish(f, self.pos)
                self._sync_audio()
        finally:
            cap.release()
            if self.audio:
                self.audio.close()


class Viewer(ctk.CTk):
    def __init__(self, start_path=None):
        super().__init__(fg_color=BG)
        self.title("Фотографии")
        self.geometry("1100x740")
        self.minsize(560, 400)
        self.after(250, self._set_icon)   # customtkinter ставит свою иконку позже

        self.files, self.idx = [], 0
        self.src = None      # исходное изображение
        self.base = None     # с учётом поворота
        self.proxy = None    # уменьшенная копия base для быстрой отрисовки
        self.rot = 0         # поворот: 0..3 (по 90° вправо)
        self.fit = True
        self.scale, self.cx, self.cy = 1.0, 0.0, 0.0
        self.photo = None
        self.message = "Откройте фотографию (Ctrl+O)"
        self._job = None
        self._drag = None
        self._moved = False
        self._fs = False
        self._last_state = "normal"

        # анимации
        self._anim = None          # текущая анимация масштаба/позиции
        self._anim_job = None
        self._fade_job = None      # кроссфейд между кадрами
        self._fade_frame = None
        self._inertia_job = None   # инерция после перетаскивания
        self._vel = (0.0, 0.0)
        self._it = 0.0
        self._hist = []
        self._last = None          # последний полностью отрисованный кадр
        self._zoom_text = None

        # видео
        self._player = None        # поток-декодер текущего видео
        self._vjob = None          # опрос новых кадров
        self._vphoto = None        # PhotoImage, который обновляется на месте
        self._vshown = None        # последний показанный кадр
        self._vdrag = False        # пользователь тянет ползунок
        self._vui_t = 0.0
        self._vtext = ""

        # полноэкранный режим
        self._hide_job = None
        self._ui_visible = False
        self._ptr = None

        # --- нижняя панель (пакуется первой, чтобы быть внизу) ---
        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=8,
                           border_width=1, border_color=BORDER)
        bar.pack(side="bottom", pady=(0, 14))
        self.bar = bar

        def icon_btn(glyph, cmd):
            b = ctk.CTkButton(bar, text=glyph, width=40, height=36, corner_radius=6,
                              font=(ICONS, 16), fg_color="transparent",
                              hover_color=HOVER, text_color=TEXT, command=cmd)
            b.pack(side="left", padx=2, pady=4)
            return b

        def separator():
            ctk.CTkFrame(bar, width=1, height=20, fg_color=BORDER).pack(side="left", padx=6)

        icon_btn("\uE838", self.open_dialog)                 # папка
        separator()
        icon_btn("\uE72B", self.prev)                        # назад
        self.counter = ctk.CTkLabel(bar, text="– / –", width=70, font=(UI, 13),
                                    text_color=TEXT)
        self.counter.pack(side="left")
        icon_btn("\uE72A", self.next)                        # вперёд
        separator()
        icon_btn("\uE71F", lambda: self.zoom_at(1 / STEP))   # отдалить
        self.zoom_btn = ctk.CTkButton(bar, text="100%", width=62, height=36,
                                      corner_radius=6, font=(UI, 13),
                                      fg_color="transparent", hover_color=HOVER,
                                      text_color=TEXT, command=self.fit_to_window)
        self.zoom_btn.pack(side="left", padx=2, pady=4)
        icon_btn("\uE8A3", lambda: self.zoom_at(STEP))       # приблизить
        separator()
        icon_btn("\uE7AD", lambda: self.rotate(1))           # повернуть
        icon_btn("\uE740", self.toggle_fullscreen)           # полный экран

        # панель видео (видна только при просмотре видео)
        self.vbox = ctk.CTkFrame(bar, fg_color="transparent")
        ctk.CTkFrame(self.vbox, width=1, height=20, fg_color=BORDER).pack(side="left", padx=6)
        self.vplay = ctk.CTkButton(self.vbox, text="\uE769", width=40, height=36,
                                   corner_radius=6, font=(ICONS, 16),
                                   fg_color="transparent", hover_color=HOVER,
                                   text_color=TEXT, command=self.toggle_pause)
        self.vplay.pack(side="left", padx=2, pady=4)
        self.vslider = ctk.CTkSlider(self.vbox, from_=0, to=1, width=180, height=16,
                                     command=self._on_vslide)
        self.vslider.pack(side="left", padx=8)
        self.vslider.bind("<ButtonPress-1>", lambda e: setattr(self, "_vdrag", True))
        self.vslider.bind("<ButtonRelease-1>", lambda e: setattr(self, "_vdrag", False))
        self.vtime = ctk.CTkLabel(self.vbox, text="0:00", width=110, font=(UI, 13),
                                  text_color=TEXT)
        self.vtime.pack(side="left", padx=(0, 6))
        self.vmute = ctk.CTkButton(self.vbox, text="\uE767", width=40, height=36,
                                   corner_radius=6, font=(ICONS, 16),
                                   fg_color="transparent", hover_color=HOVER,
                                   text_color=TEXT, command=self.toggle_mute)
        self.vmute.pack(side="left", padx=2, pady=4)
        self.vvol = ctk.CTkSlider(self.vbox, from_=0, to=1, width=80, height=16,
                                  command=self._on_vvol)
        self.vvol.set(AudioTrack.volume)
        self.vvol.pack(side="left", padx=(4, 8))

        # --- строка с информацией об изображении ---
        self.info = ctk.CTkLabel(self, text="", font=(UI, 12), text_color=SUBTLE_T)
        self.info.pack(side="bottom", pady=(0, 8))

        # --- область с изображением ---
        self.canvas = Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.bind("<Motion>", self._on_motion)

        # --- клавиши ---
        self.bind("<Left>", lambda e: self.prev())
        self.bind("<Right>", lambda e: self.next())
        self.bind("<Home>", lambda e: self.go(0))
        self.bind("<End>", lambda e: self.go(len(self.files) - 1))
        for k in ("<plus>", "<equal>", "<KP_Add>"):
            self.bind(k, lambda e: self.zoom_at(STEP))
        for k in ("<minus>", "<KP_Subtract>"):
            self.bind(k, lambda e: self.zoom_at(1 / STEP))
        self.bind("<Key-0>", lambda e: self.fit_to_window())
        for k in ("<r>", "<Key-Cyrillic_ka>"):
            self.bind(k, lambda e: self.rotate(1))
        for k in ("<R>", "<l>", "<L>", "<Key-Cyrillic_KA>", "<Key-Cyrillic_el>", "<Key-Cyrillic_EL>"):
            self.bind(k, lambda e: self.rotate(-1))
        self.bind("<Control-o>", lambda e: self.open_dialog())
        self.bind("<space>", lambda e: self.toggle_pause())
        self.bind("<Shift-Left>", lambda e: self.skip(-5))
        self.bind("<Shift-Right>", lambda e: self.skip(5))
        for k in ("<m>", "<M>", "<Key-Cyrillic_em>", "<Key-Cyrillic_EM>"):
            self.bind(k, lambda e: self.toggle_mute())
        self.bind("<Up>", lambda e: self.change_volume(0.1))
        self.bind("<Down>", lambda e: self.change_volume(-0.1))
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Escape>", lambda e: self.toggle_fullscreen(False))

        self.after(100, self._style_titlebar)
        self.after(400, self._style_titlebar)  # после внутренней настройки customtkinter

        self.protocol("WM_DELETE_WINDOW", self._close)

        if start_path and os.path.isfile(start_path):
            self.open_path(start_path)
        else:
            self.after(700, self._offer_install)

    def _set_icon(self):
        try:
            self.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass

    def _offer_install(self):
        """При первом запуске .exe без файла предлагает прописать программу в Windows."""
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            return
        cur = registered_exe()
        if cur and os.path.isfile(cur):
            return                                  # уже установлена и работает
        if cur:                                     # файл удалили/переместили - чиним молча
            register_app()
            return
        try:
            winreg, root = _hkcu()
            with winreg.OpenKey(root, rf"Software\{APP_ID}") as k:
                if winreg.QueryValueEx(k, "NoPrompt")[0]:
                    return                          # раньше отказались - не мучаем
        except OSError:
            pass
        if messagebox.askyesno(
                "PhotoViewer",
                "Сделать PhotoViewer программой для фото и видео?\n\n"
                "Он скопируется в папку программ и появится в «Приложениях по умолчанию».",
                parent=self):
            if register_app():
                messagebox.showinfo(
                    "PhotoViewer",
                    "Готово. Сейчас откроются настройки: найдите PhotoViewer и "
                    "назначьте его для нужных форматов.", parent=self)
                try:
                    os.startfile("ms-settings:defaultapps")
                except Exception:
                    pass
        else:
            try:
                _reg_set(rf"Software\{APP_ID}", "NoPrompt", 1, _hkcu()[0].REG_DWORD)
            except Exception:
                pass

    def _close(self):
        self._stop_video()
        self.destroy()

    # ---------- оформление заголовка окна (Windows 11) ----------
    def _style_titlebar(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi
            dark = ctypes.c_int(1 if DARK else 0)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)  # тёмный заголовок
            rgb = int(BG.lstrip("#"), 16)
            r, g, b = (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
            colorref = ctypes.c_int(r | (g << 8) | (b << 16))
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colorref), 4)  # цвет заголовка = фон
        except Exception:
            pass

    def _restyle_later(self):
        # Windows сбрасывает цвет заголовка при смене режима окна - применяем заново
        for ms in (50, 250, 600):
            self.after(ms, self._style_titlebar)

    # ---------- полный экран ----------
    # Используем нативный атрибут -fullscreen: он сам корректно покрывает весь
    # экран (вместе с панелью задач) и не зависит от DPI-масштабирования
    # customtkinter, которое ломало ручную установку geometry().
    def toggle_fullscreen(self, on=None):
        on = (not self._fs) if on is None else on
        if on == self._fs:
            return
        self._fs = on
        if on:
            self.bar.pack_forget()
            self.info.pack_forget()
            # Сначала сами «разворачиваем» окно (как кнопкой в заголовке),
            # и только потом включаем полный экран - иначе размер получается неверным.
            self._was_zoomed = self.state() == "zoomed"
            if not self._was_zoomed:
                self._saved_geo = self.geometry()  # размер и положение до разворота
                self.state("zoomed")
            self.update_idletasks()
            self.after(60, self._enter_fullscreen)
        else:
            if self._hide_job:
                self.after_cancel(self._hide_job)
                self._hide_job = None
            self.bar.place_forget()
            self._ui_visible = False
            self.canvas.configure(cursor="")
            self.attributes("-fullscreen", False)
            self.bar.pack(side="bottom", pady=(0, 14), before=self.canvas)
            self.info.pack(side="bottom", pady=(0, 8), before=self.canvas)
            self.update_idletasks()
            if not getattr(self, "_was_zoomed", False):
                self.after(60, self._restore_window)
            self._restyle_later()
            self.after(150, self.render)

    def _restore_window(self):
        if self._fs:
            return
        self.state("normal")
        geo = getattr(self, "_saved_geo", None)
        if geo:
            self.update_idletasks()
            self.geometry(geo)
        self.after(100, self.render)

    def _enter_fullscreen(self):
        if not self._fs:  # успели выйти, пока ждали
            return
        self.attributes("-fullscreen", True)
        self.focus_force()
        self._ptr = self.winfo_pointerxy()
        self._ui_visible = False
        self._show_ui()
        self.after(150, self.render)

    def _on_motion(self, e):
        if not self._fs:
            return
        p = (e.x_root, e.y_root)
        if self._ptr and abs(p[0] - self._ptr[0]) < 3 and abs(p[1] - self._ptr[1]) < 3:
            return  # ложное событие движения
        self._ptr = p
        self._show_ui()

    def _show_ui(self):
        if not self._ui_visible:
            self.bar.place(relx=0.5, rely=1.0, y=-20, anchor="s")
            self._ui_visible = True
        self.bar.lift()
        self.canvas.configure(cursor="")
        if self._hide_job:
            self.after_cancel(self._hide_job)
        self._hide_job = self.after(UI_HIDE_MS, self._hide_ui)

    def _hide_ui(self):
        self._hide_job = None
        if not self._fs:
            return
        px, py = self.winfo_pointerxy()
        bx, by = self.bar.winfo_rootx(), self.bar.winfo_rooty()
        if bx <= px <= bx + self.bar.winfo_width() and by <= py <= by + self.bar.winfo_height():
            self._hide_job = self.after(UI_HIDE_MS, self._hide_ui)  # курсор над панелью
            return
        self.bar.place_forget()
        self._ui_visible = False
        self.canvas.configure(cursor="none")

    # ---------- файлы ----------
    def open_dialog(self):
        exts = " ".join(f"*{e}" for e in sorted(EXTENSIONS))
        path = filedialog.askopenfilename(
            title="Открыть фото или видео",
            filetypes=[("Фото и видео", exts), ("Все файлы", "*.*")])
        if path:
            self.open_path(path)

    def open_path(self, path):
        path = os.path.abspath(path)
        folder = os.path.dirname(path)
        self.files = sorted(
            (os.path.join(folder, f) for f in os.listdir(folder)
             if os.path.splitext(f)[1].lower() in EXTENSIONS),
            key=lambda p: natural_key(os.path.basename(p)))
        self.idx = self.files.index(path) if path in self.files else 0
        self.show()

    def go(self, i):
        if self.files:
            self.idx = max(0, min(i, len(self.files) - 1))
            self.show()

    def prev(self):
        if self.files:
            self.idx = (self.idx - 1) % len(self.files)
            self.show()

    def next(self):
        if self.files:
            self.idx = (self.idx + 1) % len(self.files)
            self.show()

    # ---------- загрузка ----------
    def show(self):
        old = self._snapshot()   # кадр, из которого будем плавно переходить
        self._stop_motion()
        self._stop_video()
        path = self.files[self.idx]
        name = os.path.basename(path)
        if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
            self._show_video(path)
            return
        self.title(f"{name} — Фотографии")
        self.counter.configure(text=f"{self.idx + 1} / {len(self.files)}")
        try:
            with Image.open(path) as im:
                fmt = im.format or os.path.splitext(name)[1].lstrip(".").upper()
                exif = im.getexif()
                img = ImageOps.exif_transpose(im)
                img.load()
            if img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGBA")

            taken = None
            try:
                raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
                if raw:
                    taken = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
            except Exception:
                pass
            if taken is None:
                taken = datetime.fromtimestamp(os.path.getmtime(path))

            self.info.configure(
                text=f"{name}  ·  {img.width} × {img.height}  ·  "
                     f"{human_size(os.path.getsize(path))}  ·  {fmt}  ·  "
                     f"{taken:%d.%m.%Y %H:%M}")
            self.src, self.rot, self.fit = img, 0, True
            self._rebuild_base()
        except Exception:
            self.src = self.base = self.proxy = None
            self.message = "Не удалось открыть файл"
            self.info.configure(text=name)
        if self.base is None:
            self.render()
        else:
            self._fade(old)

    # ---------- видео ----------
    def _stop_video(self):
        if self._vjob:
            self.after_cancel(self._vjob)
            self._vjob = None
        if self._player is not None:
            self._player.running = False   # поток сам закроет файл и завершится
            if self._player.audio:
                self._player.audio.close()
            self._player = None
            self._vphoto = self._vshown = None
            self._vdrag = False
            self.vbox.pack_forget()
            self._last = None              # чтобы кроссфейд не брал устаревший кадр

    def _show_video(self, path):
        name = os.path.basename(path)
        self.title(f"{name} — Фотографии")
        self.counter.configure(text=f"{self.idx + 1} / {len(self.files)}")
        self.src = self.base = self.proxy = None
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        player = VideoPlayer(path, (cw, ch) if cw >= 10 and ch >= 10 else (1100, 700))
        if not player.ok:
            player.cap.release()
            self.message = "Не удалось открыть видео"
            self.info.configure(text=name)
            self.render()
            return
        self._player = player
        self.info.configure(
            text=f"{name}  ·  {player.w} × {player.h}  ·  {human_size(os.path.getsize(path))}  ·  "
                 f"{player.fps:.0f} к/с  ·  {os.path.splitext(name)[1].lstrip('.').upper()}")
        self.vslider.configure(state="normal" if player.total else "disabled")
        self.vslider.set(0)
        self._vtext = ""
        self.vbox.pack(side="left")
        self.canvas.delete("all")
        player.start()
        self._video_poll()

    def _video_poll(self):
        """Забирает готовый кадр у потока и рисует его. Ничего тяжёлого тут нет."""
        p = self._player
        if p is None:
            return
        if p.failed:
            self._stop_video()
            self.message = "Не удалось воспроизвести видео"
            self.render()
            return
        fr = p.frame
        if fr is not None and fr is not self._vshown:
            self._vshown = fr
            self._draw_frame(fr[0])
        self._video_ui(p)
        self._vjob = self.after(8, self._video_poll)

    def _draw_frame(self, rgb):
        h, w = rgb.shape[:2]
        cv = self.canvas
        img = Image.fromarray(rgb)
        if self._vphoto is None or (self._vphoto.width(), self._vphoto.height()) != (w, h):
            self._vphoto = ImageTk.PhotoImage(img)
            cv.delete("all")
            cv.create_image(0, 0, anchor="center", image=self._vphoto, tags="v")
        else:
            self._vphoto.paste(img)    # обновляем на месте, без создания новой картинки
        cv.coords("v", cv.winfo_width() / 2, cv.winfo_height() / 2)

    def _video_ui(self, p):
        """Ползунок, время и значок паузы (не чаще 10 раз в секунду)."""
        now = time.perf_counter()
        if now - self._vui_t < 0.1:
            return
        self._vui_t = now
        pos = p.pos
        if p.total and not self._vdrag:
            self.vslider.set(pos / max(1, p.total - 1))
        text = fmt_time(pos / p.fps)
        if p.total:
            text += " / " + fmt_time(p.total / p.fps)
        if text != self._vtext and not self._vdrag:
            self._vtext = text
            self.vtime.configure(text=text)
        glyph = "\uE768" if (p.paused or p.ended) else "\uE769"   # играть / пауза
        if self.vplay.cget("text") != glyph:
            self.vplay.configure(text=glyph)

    def toggle_pause(self):
        p = self._player
        if p is None:
            return
        if p.ended:                 # дошли до конца - начинаем сначала
            p.paused = False
            p.seek_to = 0
        else:
            p.paused = not p.paused
        self._vui_t = 0.0

    def toggle_mute(self):
        AudioTrack.muted = not AudioTrack.muted
        self._update_volume_ui()

    def change_volume(self, d):
        AudioTrack.volume = max(0.0, min(1.0, AudioTrack.volume + d))
        AudioTrack.muted = False
        self.vvol.set(AudioTrack.volume)
        self._update_volume_ui()

    def _on_vvol(self, v):
        AudioTrack.volume = float(v)
        AudioTrack.muted = False
        self._update_volume_ui()

    def _update_volume_ui(self):
        off = AudioTrack.muted or AudioTrack.volume <= 0.001
        glyph = "\uE74F" if off else "\uE767"          # без звука / звук
        if self.vmute.cget("text") != glyph:
            self.vmute.configure(text=glyph)

    def skip(self, seconds):
        p = self._player
        if p is not None:
            p.seek_to = max(0, p.pos + int(seconds * p.fps))

    def _on_vslide(self, v):
        p = self._player
        if p is None or not p.total:
            return
        n = int(v * (p.total - 1))
        p.seek_to = n
        self.vtime.configure(text=f"{fmt_time(n / p.fps)} / {fmt_time(p.total / p.fps)}")

    def _rebuild_base(self):
        ops = {0: None, 1: T.ROTATE_270, 2: T.ROTATE_180, 3: T.ROTATE_90}
        op = ops[self.rot]
        self.base = self.src.transpose(op) if op is not None else self.src
        # уменьшенная копия - чтобы анимации на больших фото не тормозили
        w, h = self.base.size
        m = max(w, h)
        if m > 2400:
            k = 2000 / m
            self.proxy = self.base.resize(
                (max(1, round(w * k)), max(1, round(h * k))),
                R.BILINEAR, reducing_gap=2.0)
        else:
            self.proxy = None

    # ---------- управление анимациями ----------
    def _stop_motion(self):
        """Останавливает все анимации. Возвращает True, если что-то шло."""
        was = bool(self._anim or self._fade_job or self._inertia_job)
        for name in ("_anim_job", "_inertia_job", "_fade_job"):
            j = getattr(self, name)
            if j:
                self.after_cancel(j)
                setattr(self, name, None)
        self._anim = None
        self._fade_frame = None
        return was

    def _start_anim(self, a):
        a["t0"] = time.perf_counter()
        self._anim = a
        self._anim_job = self.after(1, self._tick)

    def _tick(self):
        a = self._anim
        if not a:
            return
        p = min(1.0, (time.perf_counter() - a["t0"]) / a["dur"])
        e = a["ease"](p)
        s = a["s1"] if p >= 1 else a["s0"] * (a["s1"] / a["s0"]) ** e
        if a["mode"] == "zoom":
            # точка под курсором остаётся на месте
            k = s / a["s0"]
            self.cx = a["mx"] - (a["mx"] - a["cx0"]) * k
            self.cy = a["my"] - (a["my"] - a["cy0"]) * k
        else:
            self.cx = a["cx0"] + (a["cx1"] - a["cx0"]) * e
            self.cy = a["cy0"] + (a["cy1"] - a["cy0"]) * e
        self.scale = s
        if p >= 1:
            self._anim = None
            self._anim_job = None
            if a["fit"]:
                self.fit = True
            self.render()  # финальный кадр в полном качестве
        else:
            self.render(fast=True)
            self._anim_job = self.after(4, self._tick)

    # ---------- масштаб, поворот, перемещение ----------
    def rotate(self, d):
        if self.src is None:
            return
        old = self._snapshot()
        self._stop_motion()
        self.rot = (self.rot + d) % 4
        self._rebuild_base()
        self.fit = True
        self._fade(old)

    def _fit_state(self):
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        iw, ih = self.base.size
        return min(cw / iw, ch / ih, 1.0), cw / 2, ch / 2

    def fit_to_window(self):
        if self.base is None:
            return
        if self.fit and not self._anim:
            self.render()
            return
        s1, cx1, cy1 = self._fit_state()
        s0, cx0, cy0 = self.scale, self.cx, self.cy
        self._stop_motion()
        self.fit = False
        self._start_anim(dict(mode="move", dur=FIT_T, ease=ease_in_out, fit=True,
                              s0=s0, cx0=cx0, cy0=cy0, s1=s1, cx1=cx1, cy1=cy1))

    def zoom_at(self, factor, mx=None, my=None):
        if self.base is None:
            return
        if mx is None:
            mx, my = self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2
        prev = self._anim
        s_t = prev["s1"] if prev else self.scale   # накапливаем цель при быстром скролле
        s1 = max(MIN_SCALE, min(MAX_SCALE, s_t * factor))
        if abs(s1 - s_t) < 1e-9:
            return
        s0, cx0, cy0 = self.scale, self.cx, self.cy
        self._stop_motion()
        self.fit = False
        self._start_anim(dict(mode="zoom", dur=ZOOM_T, ease=ease_out, fit=False,
                              s0=s0, cx0=cx0, cy0=cy0, mx=mx, my=my, s1=s1))

    def _on_wheel(self, e):
        n = max(-3.0, min(3.0, e.delta / 120))  # тачпад даёт мелкие значения - зум плавнее
        if n:
            self.zoom_at(STEP ** n, e.x, e.y)

    def _on_double(self, e):
        if self.base is None:
            return
        if self.fit and self.scale < 0.99:
            self.zoom_at(1.0 / self.scale, e.x, e.y)  # до 100%
        elif self.fit:
            self.zoom_at(2.0, e.x, e.y)
        else:
            self.fit_to_window()

    def _drag_start(self, e):
        if self._stop_motion():
            self.render()
        self._drag = (e.x, e.y)
        self._moved = False
        self._hist = [(time.perf_counter(), e.x, e.y)]

    def _drag_move(self, e):
        if self._drag is None or self.base is None or self.fit:
            return
        self.cx += e.x - self._drag[0]
        self.cy += e.y - self._drag[1]
        self._drag = (e.x, e.y)
        self._moved = True
        now = time.perf_counter()
        self._hist.append((now, e.x, e.y))
        self._hist = [h for h in self._hist if now - h[0] < 0.12]
        self.render(fast=True)

    def _drag_end(self, _e):
        self._drag = None
        if not self._moved:
            return
        now = time.perf_counter()
        h = [p for p in self._hist if now - p[0] < 0.1]
        if len(h) >= 2 and now - h[-1][0] < 0.06 and not self.fit:
            dt = h[-1][0] - h[0][0]
            if dt > 0:
                vx = (h[-1][1] - h[0][1]) / dt
                vy = (h[-1][2] - h[0][2]) / dt
                if math.hypot(vx, vy) > 150:
                    self._vel = (vx, vy)
                    self._it = now
                    self._inertia()
                    return
        self.render()

    def _inertia(self):
        now = time.perf_counter()
        dt = min(now - self._it, 0.05)
        self._it = now
        vx, vy = self._vel
        f = math.exp(-dt * INERTIA_DECAY)
        vx, vy = vx * f, vy * f
        if math.hypot(vx, vy) < 25:
            self._inertia_job = None
            self.render()
            return
        px, py = self.cx, self.cy
        self.cx += vx * dt
        self.cy += vy * dt
        bx, by = self.cx, self.cy
        self.render(fast=True)
        # упёрлись в край - гасим скорость по этой оси
        if abs(self.cx - bx) > 0.5:
            vx = 0.0
        if abs(self.cy - by) > 0.5:
            vy = 0.0
        self._vel = (vx, vy)
        self._inertia_job = self.after(4, self._inertia)

    # ---------- отрисовка ----------
    def _on_resize(self, _e):
        if self._player is not None:
            self._player.size = (max(2, _e.width), max(2, _e.height))
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(30, self.render)
        state = self.state()
        if state != self._last_state:  # развернули/свернули окно кнопкой в заголовке
            self._last_state = state
            self._restyle_later()

    def _make_tile(self, fast):
        """Готовит видимую часть изображения. Возвращает (tile, x, y, cw, ch) или None."""
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if self.base is None or cw < 10 or ch < 10:
            return None
        iw, ih = self.base.size
        if self.fit:
            self.scale = min(cw / iw, ch / ih, 1.0)  # вписать, без увеличения
            self.cx, self.cy = cw / 2, ch / 2
        s = self.scale
        dw, dh = iw * s, ih * s

        # не даём утащить фото за пределы окна
        self.cx = cw / 2 if dw <= cw else min(dw / 2, max(cw - dw / 2, self.cx))
        self.cy = ch / 2 if dh <= ch else min(dh / 2, max(ch - dh / 2, self.cy))

        left, top = self.cx - dw / 2, self.cy - dh / 2
        x0, y0 = max(0.0, left), max(0.0, top)
        x1, y1 = min(float(cw), left + dw), min(float(ch), top + dh)
        box = (max(0.0, (x0 - left) / s), max(0.0, (y0 - top) / s),
               min(float(iw), (x1 - left) / s), min(float(ih), (y1 - top) / s))
        out = (max(1, round(x1 - x0)), max(1, round(y1 - y0)))

        src = self.base
        if fast:
            method = R.BILINEAR
            if self.proxy is not None and s < 1.0:
                src = self.proxy
                kx, ky = src.width / iw, src.height / ih
                box = (box[0] * kx, box[1] * ky, box[2] * kx, box[3] * ky)
        else:
            method = R.LANCZOS if s < 1 else (R.NEAREST if s >= 8 else R.BICUBIC)
        try:
            tile = src.resize(out, method, box=box)
        except ValueError:
            return None
        return tile, round(x0), round(y0), cw, ch

    def _show_tile(self, res):
        tile, x, y, _cw, _ch = res
        self.photo = ImageTk.PhotoImage(tile)
        cv = self.canvas
        cv.delete("all")
        cv.create_image(x, y, anchor="nw", image=self.photo)
        self._update_zoom_label()

    def _update_zoom_label(self):
        text = "–" if self.base is None else f"{round(self.scale * 100)}%"
        if text != self._zoom_text:
            self._zoom_text = text
            self.zoom_btn.configure(text=text)

    def render(self, fast=False):
        self._job = None
        if self._fade_job:  # любая «обычная» перерисовка прерывает кроссфейд
            self.after_cancel(self._fade_job)
            self._fade_job = None
            self._fade_frame = None
        if self._player is not None:  # видео рисуется отдельно (_video_poll)
            return
        cv = self.canvas
        cw, ch = cv.winfo_width(), cv.winfo_height()
        if cw < 10 or ch < 10:
            return

        if self.base is None:
            cv.delete("all")
            self._last = None
            self._update_zoom_label()
            cv.create_text(cw / 2, ch / 2, text=self.message, fill=SUBTLE,
                           font=(UI, 14))
            return

        res = self._make_tile(fast)
        if res is None:
            return
        self._show_tile(res)
        if not fast:
            self._last = res

    # ---------- плавный переход (кроссфейд) ----------
    def _composite(self, res, cw, ch):
        img = Image.new("RGB", (cw, ch), BG)
        if res is not None:
            tile, x, y = res[0], res[1], res[2]
            img.paste(tile, (x, y), tile if tile.mode == "RGBA" else None)
        return img

    def _snapshot(self):
        """Текущий вид окна как PIL-картинка (для начала кроссфейда)."""
        if self._fade_job and self._fade_frame is not None:
            return self._fade_frame
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 10 or ch < 10 or self._last is None:
            return None
        if (self._last[3], self._last[4]) != (cw, ch):
            return None
        return self._composite(self._last, cw, ch)

    def _fade(self, old):
        cv = self.canvas
        cw, ch = cv.winfo_width(), cv.winfo_height()
        if cw < 10 or ch < 10 or self.base is None:
            self.render()
            return
        new = self._make_tile(False)
        if new is None:
            self.render()
            return
        a = old if (old is not None and old.size == (cw, ch)) else Image.new("RGB", (cw, ch), BG)
        b = self._composite(new, cw, ch)
        self._last = new
        self._update_zoom_label()
        t0 = time.perf_counter()

        def step():
            p = min(1.0, (time.perf_counter() - t0) / FADE_T)
            if p >= 1:
                self._fade_job = None
                self._fade_frame = None
                self._show_tile(new)
                return
            frame = Image.blend(a, b, ease_out(p))
            self._fade_frame = frame
            self.photo = ImageTk.PhotoImage(frame)
            cv.delete("all")
            cv.create_image(0, 0, anchor="nw", image=self.photo)
            self._fade_job = self.after(4, step)

        step()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg in ("--register", "--unregister"):
        root = Tk()
        root.withdraw()
        if arg == "--register":
            if register_app():
                messagebox.showinfo("PhotoViewer", "Готово. Сейчас откроются настройки: найдите "
                                    "PhotoViewer и назначьте его для нужных форматов.")
                os.startfile("ms-settings:defaultapps")
            else:
                messagebox.showinfo("PhotoViewer", "Работает только в собранном PhotoViewer.exe.")
        else:
            unregister_app()
            messagebox.showinfo("PhotoViewer", "PhotoViewer удалён из Windows.")
        sys.exit(0)
    Viewer(arg).mainloop()