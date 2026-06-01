from setuptools import setup

APP = ["app.py"]

DATA_FILES = [
    ("static", ["static/index.html"]),
    ("", [".env"]),
]

OPTIONS = {
    "argv_emulation": False,
    "packages": [
        "uvicorn",
        "fastapi",
        "starlette",
        "requests",
        "dotenv",
        "webview",
        "pydantic",
        "anyio",
        "httptools",
        "uvloop",
    ],
    "includes": [
        "api",
        "sync_time_entries",
    ],
    "resources": [".env"],
    "plist": {
        "CFBundleName": "Productive Time Sync",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleIdentifier": "com.productive.timesync",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
}

setup(
    app=APP,
    name="Productive Time Sync",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
