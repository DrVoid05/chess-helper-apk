[app]
title = Chess Helper
package.name = chesshelper
package.domain = org.chesshelper
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.include_patterns = stockfish
version = 1.0
requirements = python3,kivy,pyjnius,chess
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = True
p4a.branch = develop

[buildozer]
log_level = 2
warn_on_root = 1
