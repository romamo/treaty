"""A tool run as a real process by test_config_layer: its env and cwd are its own"""

from treaty import App

app = App("configctl", version="1.0.0", description="Read layered config")


if __name__ == "__main__":
    app.main()
