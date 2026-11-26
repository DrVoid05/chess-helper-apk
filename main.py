import threading
import time
from kivy.app import App
from kivy.clock import Clock
from kivy.uix.label import Label
from kivy.utils import platform
from app import run_server

class ChessHelperApp(App):
    def build(self):
        threading.Thread(target=run_server, daemon=True).start()
        if platform == "android":
            Clock.schedule_once(self.open_webview, 1.5)
        return Label(text="Запуск шахматного помощника...")

    def open_webview(self, dt):
        from jnius import autoclass
        from android.runnable import run_on_ui_thread

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        WebView = autoclass("android.webkit.WebView")
        WebViewClient = autoclass("android.webkit.WebViewClient")

        @run_on_ui_thread
        def create_view():
            activity = PythonActivity.mActivity
            wv = WebView(activity)
            settings = wv.getSettings()
            settings.setJavaScriptEnabled(True)
            settings.setDomStorageEnabled(True)
            wv.setWebViewClient(WebViewClient())
            wv.loadUrl("http://127.0.0.1:5000")
            activity.setContentView(wv)

        create_view()

if __name__ == "__main__":
    ChessHelperApp().run()
