from __future__ import annotations

import io
import logging
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid

import main


class FakeTrayIcon:
    def __init__(self) -> None:
        self.visible = False
        self.stop_called = False
        self._stopped = threading.Event()

    def run(self, setup: object = None) -> None:
        if callable(setup):
            setup(self)
        self._stopped.wait(timeout=2.0)

    def stop(self) -> None:
        self.stop_called = True
        self._stopped.set()


class TrayAndLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_app = main.APP
        self.original_ui_events = main.UI_EVENTS
        main.APP = main.AppState()
        main.UI_EVENTS = queue.Queue()
        main._close_logging_handlers()

    def tearDown(self) -> None:
        main.APP.shutting_down = True
        if main.APP.tray_icon is not None:
            main.stop_tray_icon()
        main._close_logging_handlers()
        main.APP = self.original_app
        main.UI_EVENTS = self.original_ui_events

    def test_tray_image_and_menu_are_complete(self) -> None:
        image = main._create_tray_image()
        menu = main._build_tray_menu()
        actionable_items = [
            item
            for item in menu.items
            if item is not main.pystray.Menu.SEPARATOR
        ]

        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(
            [item.text for item in actionable_items],
            ["Abrir ditado", "Ver mensagens", "Encerrar"],
        )
        self.assertTrue(actionable_items[0].default)

    def test_tray_callbacks_only_publish_ui_events(self) -> None:
        events = mock.Mock()
        main.UI_EVENTS = events

        main._tray_open_dictation()
        main._tray_open_messages()
        main._tray_request_shutdown()

        self.assertEqual(
            events.put.call_args_list,
            [
                mock.call(("open_window", None)),
                mock.call(("open_log_window", None)),
                mock.call(("shutdown", None)),
            ],
        )

    def test_tray_starts_and_stops_its_own_thread(self) -> None:
        fake_icon = FakeTrayIcon()
        with mock.patch.object(main.pystray, "Icon", return_value=fake_icon):
            main.start_tray_icon()

            self.assertTrue(fake_icon.visible)
            self.assertIsNotNone(main.APP.tray_thread)
            self.assertTrue(main.APP.tray_thread.is_alive())

            main.stop_tray_icon()

        self.assertTrue(fake_icon.stop_called)
        self.assertIsNone(main.APP.tray_icon)
        self.assertIsNone(main.APP.tray_thread)

    def test_logging_reaches_console_memory_and_utf8_file(self) -> None:
        console = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                mock.patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": temporary_directory},
                ),
                mock.patch.object(main.sys, "stdout", console),
            ):
                main._configure_logging()
                main.LOGGER.info("Aplicação pronta — ação concluída.")
                revision, messages = main._log_history_snapshot()
                log_file = main.APP.log_file_path
                main._close_logging_handlers()

            self.assertGreater(revision, 0)
            self.assertIsNotNone(log_file)
            assert log_file is not None
            self.assertEqual(
                log_file,
                Path(temporary_directory)
                / "DitadoInteligente"
                / "logs"
                / "ditado-inteligente.log",
            )
            self.assertIn("Aplicação pronta — ação concluída.", console.getvalue())
            self.assertIn(
                "Aplicação pronta — ação concluída.",
                "\n".join(messages),
            )
            self.assertIn(
                "Aplicação pronta — ação concluída.",
                log_file.read_text(encoding="utf-8"),
            )

    def test_file_log_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                mock.patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": temporary_directory},
                ),
                mock.patch.object(main.sys, "stdout", None),
                mock.patch.object(main, "LOG_FILE_MAX_BYTES", 180),
            ):
                main._configure_logging()
                for index in range(20):
                    main.LOGGER.info("Registro %02d %s", index, "x" * 80)
                log_file = main.APP.log_file_path
                main._close_logging_handlers()

            assert log_file is not None
            self.assertTrue(log_file.exists())
            self.assertTrue(Path(f"{log_file}.1").exists())

    def test_logging_falls_back_to_session_when_file_fails(self) -> None:
        console = io.StringIO()
        with (
            mock.patch.object(
                main,
                "_get_log_directory",
                side_effect=OSError("sem acesso"),
            ),
            mock.patch.object(main.sys, "stdout", console),
        ):
            main._configure_logging()
            _, messages = main._log_history_snapshot()

        self.assertIsNone(main.APP.log_file_path)
        self.assertIn("sem acesso", "\n".join(messages))
        self.assertIn("sem acesso", console.getvalue())

    def test_existing_log_window_is_reused(self) -> None:
        existing_window = mock.Mock()
        existing_window.winfo_exists.return_value = True
        main.APP.root = mock.Mock()
        main.APP.log_window = existing_window

        with mock.patch.object(main.tk, "Toplevel") as toplevel:
            main.open_log_window()

        toplevel.assert_not_called()
        existing_window.lift.assert_called_once_with()
        existing_window.focus_force.assert_called_once_with()

    def test_open_log_folder_uses_persistent_log_location(self) -> None:
        main.APP.log_file_path = Path("C:/temp/logs/ditado-inteligente.log")
        with mock.patch.object(
            main.os,
            "startfile",
            create=True,
        ) as startfile:
            main.open_log_folder()

        startfile.assert_called_once_with(Path("C:/temp/logs"))

    def test_shutdown_invalidates_late_operations_and_quits_tk(self) -> None:
        main.APP.root = mock.Mock()
        main.APP.window = mock.Mock()
        main.APP.operation_id = 41
        main.APP.busy = True

        with (
            mock.patch.object(main, "_destroy_dictation_window") as destroy,
            mock.patch.object(main, "_destroy_log_window") as destroy_logs,
        ):
            main._request_application_shutdown()
            main._request_application_shutdown()

        self.assertEqual(main.APP.operation_id, 42)
        self.assertFalse(main.APP.busy)
        self.assertTrue(main.APP.shutting_down)
        destroy.assert_called_once_with()
        destroy_logs.assert_called_once_with()
        main.APP.root.quit.assert_called_once_with()

        with mock.patch.object(main, "_schedule_clipboard_copy") as schedule:
            main._finish_rewrite(41, "resposta tardia")
        schedule.assert_not_called()

    def test_cleanup_releases_ui_hotkey_and_tray_resources(self) -> None:
        root = mock.Mock()
        main.APP.operation_id = 5

        with (
            mock.patch.object(main, "_destroy_dictation_window") as destroy,
            mock.patch.object(main, "_destroy_log_window") as destroy_logs,
            mock.patch.object(main, "stop_tray_icon") as stop_tray,
            mock.patch.object(main, "stop_global_hotkey") as stop_hotkey,
        ):
            main._cleanup_runtime(root)

        self.assertEqual(main.APP.operation_id, 6)
        self.assertTrue(main.APP.shutting_down)
        destroy.assert_called_once_with()
        destroy_logs.assert_called_once_with()
        stop_tray.assert_called_once_with()
        stop_hotkey.assert_called_once_with()
        root.destroy.assert_called_once_with()
        self.assertIsNone(main.APP.root)

    def test_main_starts_tray_and_releases_instance_on_exit(self) -> None:
        root = mock.Mock()
        with (
            mock.patch.object(main, "load_dotenv"),
            mock.patch.object(main, "acquire_single_instance", return_value=True),
            mock.patch.object(main, "_configure_logging"),
            mock.patch.object(main, "_validate_startup"),
            mock.patch.object(main.tk, "Tk", return_value=root),
            mock.patch.object(main, "start_global_hotkey") as start_hotkey,
            mock.patch.object(main, "start_tray_icon") as start_tray,
            mock.patch.object(main, "_cleanup_runtime") as cleanup,
            mock.patch.object(main, "release_single_instance") as release,
            mock.patch.object(main, "_close_logging_handlers") as close_logs,
        ):
            result = main.main()

        self.assertEqual(result, 0)
        start_hotkey.assert_called_once_with()
        start_tray.assert_called_once_with()
        root.mainloop.assert_called_once_with()
        cleanup.assert_called_once_with(root)
        release.assert_called_once_with()
        close_logs.assert_called_once_with()

    def test_startup_error_uses_native_message_without_console(self) -> None:
        user32 = mock.Mock()
        error = RuntimeError("falha simulada")
        with (
            mock.patch.object(main, "_windows_user32", return_value=user32),
            mock.patch.object(main.sys, "stdout", None),
            mock.patch.object(main.sys, "stderr", None),
        ):
            main._show_startup_exception("Erro ao iniciar.", error)

        user32.MessageBoxW.assert_called_once()
        message = user32.MessageBoxW.call_args.args[1]
        flags = user32.MessageBoxW.call_args.args[3]
        self.assertIn("falha simulada", message)
        self.assertIn("Traceback completo", message)
        self.assertTrue(flags & main.MB_ICONERROR)

    @unittest.skipUnless(sys.platform == "win32", "Exclusivo do Windows")
    def test_named_mutex_detects_conflict_and_can_be_reacquired(self) -> None:
        mutex_name = rf"Local\DitadoInteligenteTeste-{uuid.uuid4()}"
        first_handle = main._create_instance_mutex(mutex_name)
        self.assertIsNotNone(first_handle)
        assert first_handle is not None

        try:
            self.assertIsNone(main._create_instance_mutex(mutex_name))
        finally:
            main._close_mutex_handle(first_handle)

        reacquired_handle = main._create_instance_mutex(mutex_name)
        self.assertIsNotNone(reacquired_handle)
        assert reacquired_handle is not None
        main._close_mutex_handle(reacquired_handle)

    def test_rewrite_worker_does_not_log_input_or_output_text(self) -> None:
        raw_text = "conteúdo ditado confidencial"
        final_text = "conteúdo reescrito confidencial"
        main.UI_EVENTS = mock.Mock()

        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                mock.patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": temporary_directory},
                ),
                mock.patch.object(main.sys, "stdout", None),
                mock.patch.object(main, "rewrite_text", return_value=final_text),
            ):
                main._configure_logging()
                main._rewrite_worker(7, raw_text)
                _, messages = main._log_history_snapshot()
                main._close_logging_handlers()

        recorded = "\n".join(messages)
        self.assertNotIn(raw_text, recorded)
        self.assertNotIn(final_text, recorded)
        main.UI_EVENTS.put.assert_called_once_with(
            ("rewrite_success", (7, final_text))
        )


if __name__ == "__main__":
    unittest.main()
