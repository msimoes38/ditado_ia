"""Ditado inteligente para Windows usando Win+H e a API da OpenAI."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from collections import deque
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import re
import sys
import threading
import traceback
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Literal

import pyautogui
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageDraw
import pystray


BASE_DIR = Path(__file__).resolve().parent
PROMPT_PATH = BASE_DIR / "prompts" / "editor_mensagens.md"

# Altere esta constante para trocar o atalho global principal.
MAIN_HOTKEY = "alt+h"

DEFAULT_MODEL = "gpt-5.4-mini"
WINDOWS_DICTATION_DELAY_MS = 450
UI_POLL_INTERVAL_MS = 50
COPY_CONFIRMATION_DELAY_MS = 1000
CLIPBOARD_START_DELAY_MS = 250
CLIPBOARD_COPY_MAX_ATTEMPTS = 5
CLIPBOARD_RETRY_DELAY_MS = 100
LOG_HISTORY_MAX_RECORDS = 2000
LOG_FILE_MAX_BYTES = 1024 * 1024
LOG_FILE_BACKUP_COUNT = 3
TRAY_START_TIMEOUT_SECONDS = 3.0
INSTANCE_MUTEX_NAME = r"Local\DitadoInteligenteOpenAI"
HOTKEY_ID = 1
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
ERROR_ALREADY_EXISTS = 183
MB_OK = 0x0000
MB_ICONERROR = 0x0010
MB_ICONINFORMATION = 0x0040
MB_TOPMOST = 0x00040000
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
LINE_BREAK_TOKEN = "[[DITADO_NOVA_LINHA]]"
PARAGRAPH_BREAK_TOKEN = "[[DITADO_NOVO_PARAGRAFO]]"
CopyKind = Literal["raw", "rewritten"]
SPOKEN_LINE_BREAK_PATTERN = re.compile(
    r"\bcomando[ \t]+nova[ \t]+linha\b[ \t]*[,.;:!?]?",
    re.IGNORECASE,
)
SPOKEN_PARAGRAPH_BREAK_PATTERN = re.compile(
    r"\bcomando[ \t]+novo[ \t]+par[aá]grafo\b[ \t]*[,.;:!?]?",
    re.IGNORECASE,
)


class ConfigurationError(RuntimeError):
    """Indica uma configuração obrigatória ausente ou inválida."""


class ClipboardError(RuntimeError):
    """Indica que o texto não pôde ser copiado."""


@dataclass
class AppState:
    root: tk.Tk | None = None
    window: tk.Toplevel | None = None
    text_box: tk.Text | None = None
    confirm_button: ttk.Button | None = None
    raw_copy_button: ttk.Button | None = None
    cancel_button: ttk.Button | None = None
    status_label: ttk.Label | None = None
    copy_confirmation_overlay: tk.Frame | None = None
    copy_confirmation_label: tk.Label | None = None
    log_window: tk.Toplevel | None = None
    log_text_box: tk.Text | None = None
    log_file_path: Path | None = None
    log_rendered_revision: int = -1
    instance_mutex_handle: int | None = None
    hotkey_thread: threading.Thread | None = None
    hotkey_thread_id: int | None = None
    hotkey_ready: threading.Event = field(default_factory=threading.Event)
    hotkey_error: str | None = None
    tray_icon: Any = None
    tray_thread: threading.Thread | None = None
    tray_ready: threading.Event = field(default_factory=threading.Event)
    tray_error: BaseException | None = None
    tray_stopping: bool = False
    operation_id: int = 0
    busy: bool = False
    closing_after_copy: bool = False
    shutting_down: bool = False


APP = AppState()
UI_EVENTS: queue.Queue[tuple[str, Any]] = queue.Queue()
LOGGER = logging.getLogger("ditado_inteligente")
LOG_HISTORY: deque[str] = deque(maxlen=LOG_HISTORY_MAX_RECORDS)
LOG_HISTORY_LOCK = threading.Lock()
LOG_HISTORY_REVISION = 0


class SessionHistoryHandler(logging.Handler):
    """Mantém um histórico limitado sem tocar em widgets Tkinter."""

    def emit(self, record: logging.LogRecord) -> None:
        global LOG_HISTORY_REVISION

        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return

        with LOG_HISTORY_LOCK:
            LOG_HISTORY.append(message)
            LOG_HISTORY_REVISION += 1


def _get_log_directory() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise OSError("A variável LOCALAPPDATA não está disponível.")
    return Path(local_app_data) / "DitadoInteligente" / "logs"


def _configure_logging() -> None:
    """Configura console, histórico da sessão e arquivo rotativo."""
    global LOG_HISTORY_REVISION

    _close_logging_handlers()
    with LOG_HISTORY_LOCK:
        LOG_HISTORY.clear()
        LOG_HISTORY_REVISION += 1

    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    history_handler = SessionHistoryHandler()
    history_handler.setFormatter(formatter)
    LOGGER.addHandler(history_handler)

    if sys.stdout is not None:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        LOGGER.addHandler(console_handler)

    APP.log_file_path = None
    try:
        log_directory = _get_log_directory()
        log_directory.mkdir(parents=True, exist_ok=True)
        log_file_path = log_directory / "ditado-inteligente.log"
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)
        APP.log_file_path = log_file_path
    except OSError as exc:
        LOGGER.warning(
            "Não foi possível habilitar o arquivo de log; as mensagens "
            "continuarão disponíveis nesta sessão: %s",
            exc,
        )


def _close_logging_handlers() -> None:
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def _log_history_snapshot() -> tuple[int, list[str]]:
    with LOG_HISTORY_LOCK:
        return LOG_HISTORY_REVISION, list(LOG_HISTORY)


def _write_console_fallback(message: str, *, error: bool = False) -> None:
    """Escreve antes do logging existir, inclusive sob pythonw."""
    stream = sys.stderr if error else sys.stdout
    if stream is None:
        stream = sys.stdout if error else sys.stderr
    if stream is None:
        return
    try:
        stream.write(f"{message}\n")
        stream.flush()
    except OSError:
        pass


def _windows_user32() -> Any:
    """Retorna user32 com assinaturas seguras também no Windows 64 bits."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.RegisterHotKey.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = ctypes.c_int
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.MessageBoxW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    ]
    user32.MessageBoxW.restype = ctypes.c_int
    return user32


def _windows_kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _create_instance_mutex(name: str = INSTANCE_MUTEX_NAME) -> int | None:
    """Cria um mutex; retorna ``None`` quando outra instância já o possui."""
    kernel32 = _windows_kernel32()
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        error_code = ctypes.get_last_error()
        raise RuntimeError(
            f"Não foi possível criar o controle de instância (código {error_code})."
        )

    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def acquire_single_instance() -> bool:
    """Adquire o mutex mantido durante toda a execução do aplicativo."""
    handle = _create_instance_mutex()
    if handle is None:
        return False
    APP.instance_mutex_handle = handle
    return True


def _close_mutex_handle(handle: int) -> None:
    _windows_kernel32().CloseHandle(handle)


def release_single_instance() -> None:
    """Libera o mutex da instância atual, se houver."""
    if APP.instance_mutex_handle is not None:
        _close_mutex_handle(APP.instance_mutex_handle)
        APP.instance_mutex_handle = None


def notify_already_running() -> None:
    """Informa no terminal e em uma caixa nativa que o app já está aberto."""
    message = "O ditado inteligente já está em execução."
    _write_console_fallback(message)
    _windows_user32().MessageBoxW(
        None,
        message,
        "Ditado inteligente",
        MB_OK | MB_ICONINFORMATION | MB_TOPMOST,
    )


def _parse_hotkey(hotkey: str) -> tuple[int, int]:
    """Converte, por exemplo, ``alt+h`` para valores da Win32 API."""
    modifier_values = {
        "alt": MOD_ALT,
        "ctrl": MOD_CONTROL,
        "control": MOD_CONTROL,
        "shift": MOD_SHIFT,
        "win": MOD_WIN,
        "windows": MOD_WIN,
    }
    parts = [part.strip().lower() for part in hotkey.split("+") if part.strip()]
    modifiers = 0
    main_keys: list[str] = []

    for part in parts:
        if part in modifier_values:
            modifiers |= modifier_values[part]
        else:
            main_keys.append(part)

    if not modifiers or len(main_keys) != 1:
        raise ConfigurationError(
            "O atalho deve ter ao menos um modificador e uma tecla, "
            "por exemplo: ctrl+alt+m ou alt+h."
        )

    main_key = main_keys[0]
    if len(main_key) == 1 and main_key.isascii() and main_key.isalnum():
        virtual_key = ord(main_key.upper())
    elif main_key.startswith("f") and main_key[1:].isdigit():
        function_number = int(main_key[1:])
        if not 1 <= function_number <= 24:
            raise ConfigurationError("A tecla de função deve estar entre F1 e F24.")
        virtual_key = 0x70 + function_number - 1
    else:
        raise ConfigurationError(
            f"A tecla '{main_key}' não é suportada no atalho. "
            "Use uma letra, número ou tecla de F1 a F24."
        )

    return modifiers, virtual_key


def _hotkey_display_name(hotkey: str) -> str:
    names = {
        "alt": "Alt",
        "ctrl": "Ctrl",
        "control": "Ctrl",
        "shift": "Shift",
        "win": "Win",
        "windows": "Win",
    }
    return " + ".join(
        names.get(part.strip().lower(), part.strip().upper())
        for part in hotkey.split("+")
        if part.strip()
    )


def start_global_hotkey() -> None:
    """Registra o atalho global por meio da API nativa do Windows."""
    if sys.platform != "win32":
        raise RuntimeError("O atalho global funciona somente no Windows.")

    modifiers, virtual_key = _parse_hotkey(MAIN_HOTKEY)
    APP.hotkey_ready.clear()
    APP.hotkey_error = None

    APP.hotkey_thread = threading.Thread(
        target=_global_hotkey_message_loop,
        args=(modifiers, virtual_key),
        name="global-hotkey-listener",
        daemon=True,
    )
    APP.hotkey_thread.start()

    if not APP.hotkey_ready.wait(timeout=2.0):
        raise RuntimeError("O Windows não respondeu ao registro do atalho.")
    if APP.hotkey_error:
        error = APP.hotkey_error
        APP.hotkey_thread.join(timeout=0.5)
        APP.hotkey_thread = None
        raise RuntimeError(error)


def stop_global_hotkey() -> None:
    """Encerra a fila de mensagens e libera o atalho registrado."""
    thread_id = APP.hotkey_thread_id
    if thread_id:
        _windows_user32().PostThreadMessageW(thread_id, WM_QUIT, 0, 0)

    if APP.hotkey_thread is not None:
        APP.hotkey_thread.join(timeout=1.0)

    APP.hotkey_thread = None
    APP.hotkey_thread_id = None
    APP.hotkey_error = None
    APP.hotkey_ready.clear()


def _global_hotkey_message_loop(modifiers: int, virtual_key: int) -> None:
    user32 = _windows_user32()
    APP.hotkey_thread_id = int(_windows_kernel32().GetCurrentThreadId())

    registered = user32.RegisterHotKey(
        None,
        HOTKEY_ID,
        modifiers | MOD_NOREPEAT,
        virtual_key,
    )
    if not registered:
        error_code = ctypes.get_last_error()
        if error_code == 1409:
            APP.hotkey_error = (
                f"O atalho {_hotkey_display_name(MAIN_HOTKEY)} já está em uso "
                "por outro aplicativo."
            )
        else:
            APP.hotkey_error = (
                "O Windows recusou o registro do atalho "
                f"(código {error_code})."
            )
        APP.hotkey_ready.set()
        APP.hotkey_thread_id = None
        return

    APP.hotkey_ready.set()
    message = wintypes.MSG()
    try:
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result == 0:
                break
            if result == -1:
                error_code = ctypes.get_last_error()
                raise OSError(
                    error_code,
                    "Falha ao ler a fila do atalho global do Windows.",
                )
            if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
                LOGGER.info("Atalho global detectado.")
                _request_dictation_window()
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)
        APP.hotkey_thread_id = None


def _create_tray_image() -> Image.Image:
    """Gera um microfone simples e legível em tamanhos pequenos."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 3, 60, 60), radius=14, fill="#1769AA")
    draw.rounded_rectangle((25, 13, 39, 38), radius=7, fill="white")
    draw.arc((18, 22, 46, 49), start=0, end=180, fill="white", width=4)
    draw.line((32, 48, 32, 53), fill="white", width=4)
    draw.line((24, 53, 40, 53), fill="white", width=4)
    return image


def _tray_open_dictation(icon: Any = None, item: Any = None) -> None:
    UI_EVENTS.put(("open_window", None))


def _tray_open_messages(icon: Any = None, item: Any = None) -> None:
    UI_EVENTS.put(("open_log_window", None))


def _tray_request_shutdown(icon: Any = None, item: Any = None) -> None:
    UI_EVENTS.put(("shutdown", None))


def _build_tray_menu() -> pystray.Menu:
    return pystray.Menu(
        pystray.MenuItem(
            "Abrir ditado",
            _tray_open_dictation,
            default=True,
        ),
        pystray.MenuItem("Ver mensagens", _tray_open_messages),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Encerrar", _tray_request_shutdown),
    )


def _tray_setup(icon: pystray.Icon) -> None:
    try:
        icon.visible = True
    except Exception as exc:
        APP.tray_error = exc
    finally:
        APP.tray_ready.set()


def _tray_run_loop() -> None:
    icon = APP.tray_icon
    if icon is None:
        return

    was_ready = False
    try:
        icon.run(setup=_tray_setup)
        was_ready = APP.tray_ready.is_set()
        if not APP.tray_stopping and not APP.shutting_down:
            raise RuntimeError("O ícone da área de notificação foi encerrado.")
    except Exception as exc:
        was_ready = APP.tray_ready.is_set()
        APP.tray_error = exc
        APP.tray_ready.set()
        LOGGER.error(
            "Falha no ícone da área de notificação.\n\n"
            "Traceback completo:\n%s",
            _format_exception(exc),
        )
        if was_ready and not APP.tray_stopping and not APP.shutting_down:
            UI_EVENTS.put(("tray_error", exc))


def start_tray_icon() -> None:
    """Inicia o loop nativo da bandeja fora da thread do Tkinter."""
    APP.tray_ready.clear()
    APP.tray_error = None
    APP.tray_stopping = False
    APP.tray_icon = pystray.Icon(
        "ditado_inteligente",
        icon=_create_tray_image(),
        title=(
            "Ditado inteligente — ativo "
            f"({_hotkey_display_name(MAIN_HOTKEY)})"
        ),
        menu=_build_tray_menu(),
    )
    APP.tray_thread = threading.Thread(
        target=_tray_run_loop,
        name="system-tray",
        daemon=True,
    )
    APP.tray_thread.start()

    if not APP.tray_ready.wait(timeout=TRAY_START_TIMEOUT_SECONDS):
        raise RuntimeError(
            "O Windows não confirmou a criação do ícone da área de notificação."
        )
    if APP.tray_error is not None:
        raise RuntimeError(
            "Não foi possível criar o ícone da área de notificação."
        ) from APP.tray_error


def stop_tray_icon() -> None:
    """Remove o ícone e encerra sua thread, se ela estiver ativa."""
    APP.tray_stopping = True
    icon = APP.tray_icon
    thread = APP.tray_thread

    if icon is not None:
        try:
            icon.stop()
        except Exception as exc:
            LOGGER.error(
                "Falha ao remover o ícone da área de notificação.\n\n"
                "Traceback completo:\n%s",
                _format_exception(exc),
            )

    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2.0)
        if thread.is_alive():
            LOGGER.warning(
                "A thread do ícone da área de notificação não encerrou no prazo."
            )

    APP.tray_icon = None
    APP.tray_thread = None
    APP.tray_error = None
    APP.tray_ready.clear()


def load_prompt() -> str:
    """Carrega o prompt fixo usado para editar o texto ditado."""
    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(
            f"Não foi possível ler o prompt em {PROMPT_PATH}."
        ) from exc

    if not prompt:
        raise ConfigurationError("O arquivo de prompt está vazio.")
    return prompt


def encode_spoken_structure_commands(raw_text: str) -> str:
    """Troca comandos falados por marcadores que devem atravessar a API."""
    encoded_text = SPOKEN_PARAGRAPH_BREAK_PATTERN.sub(
        f" {PARAGRAPH_BREAK_TOKEN} ",
        raw_text,
    )
    return SPOKEN_LINE_BREAK_PATTERN.sub(
        f" {LINE_BREAK_TOKEN} ",
        encoded_text,
    )


def restore_structure_tokens(text: str) -> str:
    """Converte os marcadores preservados pelo modelo em quebras reais."""
    paragraph_pattern = re.compile(
        rf"[ \t]*{re.escape(PARAGRAPH_BREAK_TOKEN)}[ \t]*",
        re.IGNORECASE,
    )
    line_pattern = re.compile(
        rf"[ \t]*{re.escape(LINE_BREAK_TOKEN)}[ \t]*",
        re.IGNORECASE,
    )
    restored_text = paragraph_pattern.sub("\n\n", text)
    return line_pattern.sub("\n", restored_text)


def rewrite_text(raw_text: str) -> str:
    """Reescreve somente texto por meio da Responses API da OpenAI."""
    raw_text = raw_text.strip()
    if not raw_text:
        raise ValueError("O texto ditado está vazio.")

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ConfigurationError(
            "OPENAI_API_KEY não foi configurada no arquivo .env ou no ambiente."
        )

    model = os.getenv("OPENAI_TEXT_MODEL", DEFAULT_MODEL).strip()
    if not model:
        model = DEFAULT_MODEL

    encoded_text = encode_spoken_structure_commands(raw_text)
    client = OpenAI(api_key=api_key, timeout=60.0, max_retries=2)
    response = client.responses.create(
        model=model,
        instructions=load_prompt(),
        input=encoded_text,
    )

    final_text = response.output_text.strip()
    if not final_text:
        raise RuntimeError("A API retornou uma resposta sem texto.")
    return restore_structure_tokens(final_text)


def trigger_windows_dictation() -> None:
    """Aciona o ditado nativo do Windows com Win+H."""
    # RegisterHotKey notifica no pressionamento. Aguarde o usuário soltar os
    # modificadores para que o Windows receba somente Win+H.
    if _hotkey_modifiers_are_pressed():
        if APP.window is not None and APP.window.winfo_exists():
            APP.window.after(50, trigger_windows_dictation)
        return

    try:
        pyautogui.hotkey("win", "h")
    except Exception as exc:
        _show_exception(
            "Falha ao abrir o ditado",
            "Não foi possível enviar o atalho Win + H.",
            exc,
        )


def _hotkey_modifiers_are_pressed() -> bool:
    if sys.platform != "win32":
        return False

    user32 = _windows_user32()
    virtual_keys = (0x10, 0x11, 0x12, 0x5B, 0x5C)
    return any(user32.GetAsyncKeyState(key) & 0x8000 for key in virtual_keys)


def open_dictation_window() -> None:
    """Abre a janela intermediária, pronta para receber o ditado."""
    if APP.shutting_down:
        return
    if APP.window is not None and APP.window.winfo_exists():
        APP.window.lift()
        if APP.text_box is not None:
            APP.text_box.focus_force()
        return

    if APP.root is None:
        return

    APP.operation_id += 1
    APP.busy = False
    APP.closing_after_copy = False

    window = tk.Toplevel(APP.root)
    APP.window = window
    window.title("Ditado inteligente")
    window.geometry("640x400")
    window.minsize(520, 330)
    window.protocol("WM_DELETE_WINDOW", cancel_operation)
    window.bind("<Escape>", cancel_operation)
    window.bind("<Control-Return>", _confirm_shortcut)

    container = ttk.Frame(window, padding=14)
    container.pack(fill="both", expand=True)
    container.columnconfigure(0, weight=1)
    container.rowconfigure(1, weight=1)

    instruction = ttk.Label(
        container,
        text=(
            "Dite usando o recurso do Windows. Pressione Ctrl + Enter para "
            "reescrever, use Copiar texto bruto ou pressione Esc para cancelar. "
            "Diga 'comando nova linha' ou 'comando novo parágrafo' para "
            "estruturar o texto tratado."
        ),
        wraplength=600,
        justify="left",
    )
    instruction.grid(row=0, column=0, sticky="ew", pady=(0, 10))

    text_frame = ttk.Frame(container)
    text_frame.grid(row=1, column=0, sticky="nsew")
    text_frame.columnconfigure(0, weight=1)
    text_frame.rowconfigure(0, weight=1)

    text_box = tk.Text(
        text_frame,
        wrap="word",
        undo=True,
        font=("Segoe UI", 11),
        padx=8,
        pady=8,
    )
    APP.text_box = text_box
    scrollbar = ttk.Scrollbar(
        text_frame,
        orient="vertical",
        command=text_box.yview,
    )
    text_box.configure(yscrollcommand=scrollbar.set)
    text_box.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns")

    footer = ttk.Frame(container)
    footer.grid(row=2, column=0, sticky="ew", pady=(10, 0))
    footer.columnconfigure(0, weight=1)

    status_label = ttk.Label(footer, text="Aguardando ditado...")
    APP.status_label = status_label
    status_label.grid(row=0, column=0, sticky="ew")

    actions = ttk.Frame(footer)
    actions.grid(row=1, column=0, sticky="e", pady=(8, 0))

    raw_copy_button = ttk.Button(
        actions,
        text="Copiar texto bruto",
        command=copy_raw_text,
    )
    APP.raw_copy_button = raw_copy_button
    raw_copy_button.pack(side="left")

    confirm_button = ttk.Button(
        actions,
        text="Reescrever e copiar",
        command=start_rewrite,
    )
    APP.confirm_button = confirm_button
    confirm_button.pack(side="left", padx=(8, 0))

    cancel_button = ttk.Button(
        actions,
        text="Cancelar",
        command=cancel_operation,
    )
    APP.cancel_button = cancel_button
    cancel_button.pack(side="left", padx=(8, 0))

    window.update_idletasks()
    _center_window(window)
    window.lift()
    text_box.focus_force()
    window.after(WINDOWS_DICTATION_DELAY_MS, trigger_windows_dictation)

    LOGGER.info("Janela de ditado aberta.")


def start_rewrite() -> None:
    """Valida o conteúdo e inicia a chamada da API fora da thread da UI."""
    if APP.busy or APP.text_box is None:
        return

    raw_text = APP.text_box.get("1.0", "end-1c").strip()
    if not raw_text:
        messagebox.showwarning(
            "Texto vazio",
            "Dite ou digite algum texto antes de confirmar.",
            parent=APP.window,
        )
        APP.text_box.focus_force()
        return

    APP.busy = True
    _set_controls_enabled(False)
    _move_focus_away_from_dictation()
    if APP.status_label is not None:
        APP.status_label.configure(text="Reescrevendo texto...")
    LOGGER.info("Reescrevendo texto...")

    operation_id = APP.operation_id
    thread = threading.Thread(
        target=_rewrite_worker,
        args=(operation_id, raw_text),
        daemon=True,
    )
    thread.start()


def copy_raw_text() -> None:
    """Copia exatamente o texto da caixa e fecha imediatamente."""
    if APP.busy or APP.closing_after_copy or APP.text_box is None:
        return

    raw_text = APP.text_box.get("1.0", "end-1c")
    if not raw_text.strip():
        messagebox.showwarning(
            "Texto vazio",
            "Dite ou digite algum texto antes de copiar.",
            parent=APP.window,
        )
        APP.text_box.focus_force()
        return

    APP.busy = True
    _set_controls_enabled(False)
    _move_focus_away_from_dictation()
    if APP.status_label is not None:
        APP.status_label.configure(text="Copiando texto...")
    _schedule_clipboard_copy(APP.operation_id, raw_text, "raw")


def cancel_operation(event: tk.Event | None = None) -> str | None:
    """Fecha a janela sem copiar e invalida respostas ainda em andamento."""
    if APP.closing_after_copy:
        return "break" if event is not None else None

    APP.operation_id += 1
    APP.busy = False
    _destroy_dictation_window()
    LOGGER.info("Operação cancelada.")
    return "break" if event is not None else None


def _rewrite_worker(operation_id: int, raw_text: str) -> None:
    try:
        final_text = rewrite_text(raw_text)
    except Exception as exc:
        traceback_details = _format_exception(exc)
        UI_EVENTS.put(("rewrite_error", (operation_id, exc, traceback_details)))
    else:
        UI_EVENTS.put(("rewrite_success", (operation_id, final_text)))


def _move_focus_away_from_dictation() -> None:
    """Encerra o foco de entrada usado pelo painel Win+H."""
    if APP.cancel_button is not None:
        APP.cancel_button.focus_set()


def _schedule_clipboard_copy(
    operation_id: int,
    text: str,
    copy_kind: CopyKind,
) -> None:
    """Dá tempo ao Windows para encerrar o ditado antes de copiar."""
    if APP.window is None:
        return
    APP.window.after(
        CLIPBOARD_START_DELAY_MS,
        lambda: _copy_to_clipboard(operation_id, text, copy_kind),
    )


def _finish_rewrite(operation_id: int, final_text: str) -> None:
    if operation_id != APP.operation_id or APP.window is None:
        return

    if APP.status_label is not None:
        APP.status_label.configure(text="Copiando texto...")
    _schedule_clipboard_copy(operation_id, final_text, "rewritten")


def _handle_clipboard_success(
    operation_id: int,
    copy_kind: CopyKind,
) -> None:
    if operation_id != APP.operation_id or APP.window is None:
        return

    if copy_kind == "raw":
        _destroy_dictation_window()
        APP.busy = False
        LOGGER.info(
            "Texto bruto copiado para a área de transferência. "
            "Pressione Ctrl + V onde desejar colá-lo."
        )
        return

    APP.closing_after_copy = True
    _set_controls_enabled(False, allow_cancel=False)
    if APP.status_label is not None:
        APP.status_label.configure(text="Texto copiado! Pressione Ctrl + V.")
    if APP.window is not None:
        APP.window.protocol("WM_DELETE_WINDOW", lambda: None)
        _show_copy_confirmation()
        APP.window.after(
            COPY_CONFIRMATION_DELAY_MS,
            lambda: _complete_copy_confirmation(operation_id),
        )
    LOGGER.info(
        "Texto copiado para a área de transferência. Pressione Ctrl + V "
        "onde desejar colá-lo."
    )


def _copy_to_clipboard(
    operation_id: int,
    text: str,
    copy_kind: CopyKind,
    attempt: int = 1,
) -> None:
    """Copia pela janela Tk na thread da UI, com tentativas curtas."""
    if (
        operation_id != APP.operation_id
        or APP.window is None
        or not APP.busy
    ):
        return

    try:
        APP.window.clipboard_clear()
        APP.window.clipboard_append(text)
        APP.window.update_idletasks()
    except Exception as tk_error:
        if attempt < CLIPBOARD_COPY_MAX_ATTEMPTS:
            APP.window.after(
                CLIPBOARD_RETRY_DELAY_MS,
                lambda: _copy_to_clipboard(
                    operation_id,
                    text,
                    copy_kind,
                    attempt + 1,
                ),
            )
            return

        try:
            raise ClipboardError(
                "Não foi possível copiar o texto pela interface Tkinter "
                f"após {CLIPBOARD_COPY_MAX_ATTEMPTS} tentativas."
            ) from tk_error
        except ClipboardError as clipboard_error:
            traceback_details = _format_exception(clipboard_error)
            _handle_clipboard_error(
                operation_id,
                clipboard_error,
                traceback_details,
            )
        return

    _handle_clipboard_success(operation_id, copy_kind)


def _show_copy_confirmation() -> None:
    """Exibe uma confirmação central e inequivocamente visível."""
    if APP.window is None:
        return

    overlay = tk.Frame(
        APP.window,
        background="#EAF7EE",
        highlightbackground="#4A8F5B",
        highlightthickness=2,
    )
    APP.copy_confirmation_overlay = overlay
    overlay.place(relx=0, rely=0, relwidth=1, relheight=1)

    content = tk.Frame(overlay, background="#EAF7EE")
    content.place(relx=0.5, rely=0.5, anchor="center")

    check_label = tk.Label(
        content,
        text="✓",
        font=("Segoe UI", 32, "bold"),
        foreground="#237A3B",
        background="#EAF7EE",
    )
    check_label.pack()

    confirmation_label = tk.Label(
        content,
        text="Texto copiado!\nPressione Ctrl + V.",
        font=("Segoe UI", 16, "bold"),
        foreground="#184F28",
        background="#EAF7EE",
        justify="center",
    )
    APP.copy_confirmation_label = confirmation_label
    confirmation_label.pack(pady=(8, 0))

    overlay.lift()
    APP.window.attributes("-topmost", True)
    APP.window.lift()
    APP.window.focus_force()
    APP.window.update_idletasks()


def _complete_copy_confirmation(operation_id: int) -> None:
    if operation_id != APP.operation_id or not APP.closing_after_copy:
        return
    _destroy_dictation_window()
    APP.busy = False


def _handle_clipboard_error(
    operation_id: int,
    exc: ClipboardError,
    traceback_details: str,
) -> None:
    if operation_id != APP.operation_id or APP.window is None:
        return

    APP.busy = False
    APP.closing_after_copy = False
    _set_controls_enabled(True)
    if APP.status_label is not None:
        APP.status_label.configure(text="Não foi possível copiar o texto.")
    _show_exception(
        "Falha ao copiar",
        "Não foi possível copiar o texto.",
        exc,
        traceback_details,
    )
    if APP.text_box is not None:
        APP.text_box.focus_force()


def _handle_rewrite_error(
    operation_id: int,
    exc: Exception,
    traceback_details: str,
) -> None:
    if operation_id != APP.operation_id or APP.window is None:
        return

    APP.busy = False
    _set_controls_enabled(True)
    if APP.status_label is not None:
        APP.status_label.configure(text="Não foi possível reescrever.")

    _show_exception(
        "Falha na reescrita",
        "Não foi possível reescrever o texto.",
        exc,
        traceback_details,
    )
    if APP.text_box is not None:
        APP.text_box.focus_force()


def open_log_window() -> None:
    """Abre o histórico da sessão sem bloquear o restante da aplicação."""
    if APP.shutting_down or APP.root is None:
        return
    if APP.log_window is not None and APP.log_window.winfo_exists():
        APP.log_window.lift()
        APP.log_window.focus_force()
        return

    window = tk.Toplevel(APP.root)
    APP.log_window = window
    APP.log_rendered_revision = -1
    window.title("Mensagens da aplicação")
    window.geometry("820x480")
    window.minsize(620, 340)
    window.protocol("WM_DELETE_WINDOW", _destroy_log_window)
    window.bind("<Escape>", lambda event: _destroy_log_window())

    container = ttk.Frame(window, padding=14)
    container.pack(fill="both", expand=True)
    container.columnconfigure(0, weight=1)
    container.rowconfigure(1, weight=1)

    if APP.log_file_path is None:
        path_message = (
            "Arquivo persistente indisponível; o histórico abaixo vale "
            "somente para esta execução."
        )
    else:
        path_message = f"Arquivo atual: {APP.log_file_path}"
    ttk.Label(
        container,
        text=path_message,
        wraplength=780,
        justify="left",
    ).grid(row=0, column=0, sticky="ew", pady=(0, 10))

    text_frame = ttk.Frame(container)
    text_frame.grid(row=1, column=0, sticky="nsew")
    text_frame.columnconfigure(0, weight=1)
    text_frame.rowconfigure(0, weight=1)

    text_box = tk.Text(
        text_frame,
        wrap="none",
        font=("Consolas", 10),
        padx=8,
        pady=8,
        state="disabled",
    )
    APP.log_text_box = text_box
    vertical_scrollbar = ttk.Scrollbar(
        text_frame,
        orient="vertical",
        command=text_box.yview,
    )
    horizontal_scrollbar = ttk.Scrollbar(
        text_frame,
        orient="horizontal",
        command=text_box.xview,
    )
    text_box.configure(
        yscrollcommand=vertical_scrollbar.set,
        xscrollcommand=horizontal_scrollbar.set,
    )
    text_box.grid(row=0, column=0, sticky="nsew")
    vertical_scrollbar.grid(row=0, column=1, sticky="ns")
    horizontal_scrollbar.grid(row=1, column=0, sticky="ew")

    actions = ttk.Frame(container)
    actions.grid(row=2, column=0, sticky="e", pady=(10, 0))
    open_folder_button = ttk.Button(
        actions,
        text="Abrir pasta dos logs",
        command=open_log_folder,
    )
    if APP.log_file_path is None:
        open_folder_button.configure(state="disabled")
    open_folder_button.pack(side="left")
    ttk.Button(
        actions,
        text="Fechar",
        command=_destroy_log_window,
    ).pack(side="left", padx=(8, 0))

    window.update_idletasks()
    _center_window(window)
    window.lift()
    window.focus_force()
    _refresh_log_viewer()
    LOGGER.info("Janela de mensagens aberta.")


def open_log_folder() -> None:
    if APP.log_file_path is None:
        return

    try:
        os.startfile(APP.log_file_path.parent)  # type: ignore[attr-defined]
    except OSError as exc:
        _show_exception(
            "Falha ao abrir os logs",
            "Não foi possível abrir a pasta dos logs.",
            exc,
        )


def _refresh_log_viewer() -> None:
    if APP.log_window is None or APP.log_text_box is None:
        return
    try:
        if not APP.log_window.winfo_exists():
            return
    except tk.TclError:
        return

    revision, messages = _log_history_snapshot()
    if revision == APP.log_rendered_revision:
        return

    APP.log_text_box.configure(state="normal")
    APP.log_text_box.delete("1.0", "end")
    APP.log_text_box.insert(
        "1.0",
        "\n".join(messages) if messages else "Nenhuma mensagem registrada.",
    )
    APP.log_text_box.configure(state="disabled")
    APP.log_text_box.see("end")
    APP.log_rendered_revision = revision


def _poll_ui_events() -> None:
    while True:
        try:
            event_name, payload = UI_EVENTS.get_nowait()
        except queue.Empty:
            break

        if event_name == "open_window":
            open_dictation_window()
        elif event_name == "open_log_window":
            open_log_window()
        elif event_name == "shutdown":
            _request_application_shutdown()
            break
        elif event_name == "rewrite_success":
            operation_id, final_text = payload
            _finish_rewrite(operation_id, final_text)
        elif event_name == "rewrite_error":
            operation_id, exc, traceback_details = payload
            _handle_rewrite_error(operation_id, exc, traceback_details)
        elif event_name == "background_error":
            title, message = payload
            _show_error(title, message)
        elif event_name == "tray_error":
            details = _format_exception(payload)
            _show_error(
                "Falha na área de notificação",
                "O ícone da área de notificação parou de funcionar e a "
                "aplicação será encerrada."
                f"\n\nTraceback completo:\n{details}",
            )
            _request_application_shutdown()
            break

    _refresh_log_viewer()
    if APP.root is not None and not APP.shutting_down:
        APP.root.after(UI_POLL_INTERVAL_MS, _poll_ui_events)


def _request_dictation_window() -> None:
    UI_EVENTS.put(("open_window", None))


def _confirm_shortcut(event: tk.Event) -> str:
    start_rewrite()
    return "break"


def _set_controls_enabled(enabled: bool, allow_cancel: bool = True) -> None:
    state = "normal" if enabled else "disabled"
    if APP.text_box is not None:
        APP.text_box.configure(state=state)
    if APP.confirm_button is not None:
        APP.confirm_button.configure(state=state)
    if APP.raw_copy_button is not None:
        APP.raw_copy_button.configure(state=state)
    if APP.cancel_button is not None:
        cancel_state = "normal" if enabled or allow_cancel else "disabled"
        APP.cancel_button.configure(state=cancel_state)


def _destroy_dictation_window() -> None:
    if APP.window is not None:
        try:
            if APP.window.winfo_exists():
                APP.window.destroy()
        except tk.TclError as exc:
            _print_exception("Falha ao fechar a janela de ditado.", exc)

    APP.window = None
    APP.text_box = None
    APP.confirm_button = None
    APP.raw_copy_button = None
    APP.cancel_button = None
    APP.status_label = None
    APP.copy_confirmation_overlay = None
    APP.copy_confirmation_label = None
    APP.closing_after_copy = False


def _destroy_log_window() -> None:
    if APP.log_window is not None:
        try:
            if APP.log_window.winfo_exists():
                APP.log_window.destroy()
        except tk.TclError as exc:
            _print_exception("Falha ao fechar a janela de mensagens.", exc)

    APP.log_window = None
    APP.log_text_box = None
    APP.log_rendered_revision = -1


def _request_application_shutdown() -> None:
    """Solicita uma saída limpa a partir da thread principal do Tkinter."""
    if APP.shutting_down:
        return

    APP.shutting_down = True
    APP.operation_id += 1
    APP.busy = False
    LOGGER.info("Encerrando a aplicação...")
    _destroy_dictation_window()
    _destroy_log_window()
    if APP.root is not None:
        APP.root.quit()


def _center_window(window: tk.Toplevel) -> None:
    width = window.winfo_width()
    height = window.winfo_height()
    x = max(0, (window.winfo_screenwidth() - width) // 2)
    y = max(0, (window.winfo_screenheight() - height) // 3)
    window.geometry(f"{width}x{height}+{x}+{y}")


def _format_exception(exc: BaseException) -> str:
    """Formata a exceção e toda a cadeia causal com seu traceback."""
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__, chain=True)
    ).rstrip()


def _print_exception(
    context: str,
    exc: BaseException,
    traceback_details: str | None = None,
) -> None:
    details = traceback_details or _format_exception(exc)
    message = f"{context}\n\nTraceback completo:\n{details}"
    if LOGGER.handlers:
        LOGGER.error(message)
    else:
        _write_console_fallback(message, error=True)


def _show_exception(
    title: str,
    message: str,
    exc: BaseException,
    traceback_details: str | None = None,
) -> None:
    """Exibe e registra uma falha sem omitir o traceback original."""
    details = traceback_details or _format_exception(exc)
    full_message = f"{message}\n\nTraceback completo:\n{details}"
    if LOGGER.handlers:
        LOGGER.error("%s: %s", title, full_message)
    else:
        _write_console_fallback(f"{title}: {full_message}", error=True)
    _show_error(title, full_message)


def _report_tk_callback_exception(
    exc_type: type[BaseException],
    exc: BaseException,
    exc_traceback: Any,
) -> None:
    """Encaminha exceções não tratadas de callbacks do Tkinter à interface."""
    traceback_details = "".join(
        traceback.format_exception(exc_type, exc, exc_traceback, chain=True)
    ).rstrip()
    _show_exception(
        "Erro inesperado",
        "Ocorreu um erro inesperado na interface.",
        exc,
        traceback_details,
    )


def _report_thread_exception(args: Any) -> None:
    """Registra exceções não tratadas das threads e avisa a interface."""
    if args.exc_type is SystemExit:
        return

    traceback_details = "".join(
        traceback.format_exception(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            chain=True,
        )
    ).rstrip()
    thread_name = args.thread.name if args.thread is not None else "desconhecida"
    title = "Erro em tarefa de segundo plano"
    message = (
        f"Ocorreu um erro inesperado na thread '{thread_name}'."
        f"\n\nTraceback completo:\n{traceback_details}"
    )
    if LOGGER.handlers:
        LOGGER.error("%s: %s", title, message)
    else:
        _write_console_fallback(f"{title}: {message}", error=True)
    if APP.root is not None:
        UI_EVENTS.put(("background_error", (title, message)))


def _show_error(title: str, message: str) -> None:
    try:
        messagebox.showerror(title, message, parent=APP.window or APP.root)
    except tk.TclError as exc:
        if LOGGER.handlers:
            LOGGER.error("%s: %s", title, message)
        else:
            _write_console_fallback(f"{title}: {message}", error=True)
        _print_exception("Falha ao exibir a caixa de erro.", exc)


def _show_startup_exception(context: str, exc: BaseException) -> None:
    """Registra e exibe uma falha mesmo quando não existe console ou Tk."""
    details = _format_exception(exc)
    _print_exception(context, exc, details)
    log_hint = (
        f"\n\nArquivo de log: {APP.log_file_path}"
        if APP.log_file_path is not None
        else ""
    )
    message = (
        f"{context}\n\n{exc}\n\nTraceback completo:\n{details}{log_hint}"
    )
    try:
        _windows_user32().MessageBoxW(
            None,
            message,
            "Ditado inteligente",
            MB_OK | MB_ICONERROR | MB_TOPMOST,
        )
    except Exception as notification_error:
        _write_console_fallback(
            "Também não foi possível exibir a mensagem nativa de erro: "
            f"{notification_error}",
            error=True,
        )


def _validate_startup() -> None:
    if sys.platform != "win32":
        raise ConfigurationError("Este projeto foi desenvolvido para Windows.")

    if not os.getenv("OPENAI_API_KEY", "").strip():
        raise ConfigurationError(
            "OPENAI_API_KEY não foi configurada. Preencha o arquivo .env "
            "antes de executar o programa."
        )

    load_prompt()
    _parse_hotkey(MAIN_HOTKEY)


def _cleanup_runtime(root: tk.Tk) -> None:
    """Encerra todos os recursos que pertencem à instância atual."""
    if not APP.shutting_down:
        APP.operation_id += 1
    APP.shutting_down = True
    APP.busy = False
    _destroy_dictation_window()
    _destroy_log_window()
    stop_tray_icon()
    stop_global_hotkey()
    try:
        root.destroy()
    except tk.TclError as exc:
        _print_exception("Falha ao encerrar a interface.", exc)
    APP.root = None


def main() -> int:
    """Configura a interface oculta e registra o atalho global."""
    load_dotenv(BASE_DIR / ".env")
    threading.excepthook = _report_thread_exception
    APP.shutting_down = False

    try:
        acquired = acquire_single_instance()
    except RuntimeError as exc:
        _show_startup_exception("Erro ao iniciar.", exc)
        return 1

    if not acquired:
        notify_already_running()
        return 1

    try:
        _configure_logging()
        LOGGER.info("Iniciando o ditado inteligente.")
        try:
            _validate_startup()
        except ConfigurationError as exc:
            _show_startup_exception("Erro de configuração.", exc)
            return 1

        try:
            root = tk.Tk()
        except Exception as exc:
            _show_startup_exception("Não foi possível iniciar a interface.", exc)
            return 1

        APP.root = root
        root.report_callback_exception = _report_tk_callback_exception
        root.withdraw()

        try:
            try:
                start_global_hotkey()
                start_tray_icon()
            except Exception as exc:
                _show_startup_exception(
                    "Não foi possível concluir a inicialização.",
                    exc,
                )
                return 1

            root.after(UI_POLL_INTERVAL_MS, _poll_ui_events)
            LOGGER.info(
                "Aguardando atalho %s...",
                _hotkey_display_name(MAIN_HOTKEY),
            )
            if sys.stdout is not None:
                LOGGER.info(
                    "Pressione Ctrl + C no terminal ou use Encerrar no ícone "
                    "da área de notificação."
                )

            try:
                root.mainloop()
            except KeyboardInterrupt:
                LOGGER.info("Interrupção recebida pelo terminal.")
        finally:
            _cleanup_runtime(root)

        return 0
    finally:
        release_single_instance()
        if LOGGER.handlers:
            LOGGER.info("Aplicação encerrada.")
        _close_logging_handlers()


if __name__ == "__main__":
    raise SystemExit(main())
