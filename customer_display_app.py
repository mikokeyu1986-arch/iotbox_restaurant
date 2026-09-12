import argparse
import webview


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--x", type=int, required=True)
    parser.add_argument("--y", type=int, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    args = parser.parse_args()
    webview.create_window(
        "Pantalla del cliente",
        url=args.url,
        x=args.x,
        y=args.y,
        width=args.width,
        height=args.height,
        fullscreen=True,
        resizable=False,
    )
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
