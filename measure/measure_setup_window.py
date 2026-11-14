"""Measure where pywebview puts the 980x780 setup window on this Windows screen (read-only probe).

Prints one JSON line: the form's outer bounds, its client area in screen coordinates, the screen and its working
area (all in the process's pixels), the system scale, and how many pixels of the page lie below the working area.
With ``--fit module:function`` the named function is called with the window before ``webview.start`` (the fix
under test attaches itself there, as the product does).
"""
import argparse
import importlib
import json
import sys
import threading
import time

import webview

PAGE = """<!doctype html><html><body style="margin:0;font:14px sans-serif">
<div style="min-height:1600px;padding:12px">step content longer than the window</div>
<footer id="foot" style="position:sticky;bottom:0;padding:12px;background:#ddd">
<button id="continue" style="height:34px">Continue</button></footer></body></html>"""


def read_geometry(window, out):
    from System import Action
    from System.Drawing import Point
    from System.Windows.Forms import Screen, SystemInformation

    form = window.native

    def read():
        screen = Screen.FromControl(form)
        bounds, work, whole = form.Bounds, screen.WorkingArea, screen.Bounds
        origin = form.PointToScreen(Point(0, 0))
        client = form.ClientSize
        out.update(
            form=[bounds.X, bounds.Y, bounds.Width, bounds.Height],
            client=[origin.X, origin.Y, client.Width, client.Height],
            screen=[whole.X, whole.Y, whole.Width, whole.Height],
            work=[work.X, work.Y, work.Width, work.Height],
            min_size=[form.MinimumSize.Width, form.MinimumSize.Height],
            max_track=[SystemInformation.MaxWindowTrackSize.Width, SystemInformation.MaxWindowTrackSize.Height],
            window_state=str(form.WindowState),
            device_dpi=int(getattr(form, "DeviceDpi", 0) or 0),
            page_below_work=max(0, origin.Y + client.Height - (work.Y + work.Height)),
            form_below_work=max(0, bounds.Y + bounds.Height - (work.Y + work.Height)),
            form_above_work=max(0, work.Y - bounds.Y),
        )

    form.Invoke(Action(read))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fit", default="")
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    out = {"label": args.label, "fit": args.fit, "pywebview": getattr(webview, "__version__", "?")}
    window = webview.create_window("Ouroboros — Setup", html=PAGE, width=980, height=780, min_size=(840, 640))
    if args.fit:
        module, _, name = args.fit.partition(":")
        getattr(importlib.import_module(module), name)(window)

    def probe():
        try:
            window.events.shown.wait(60)
            time.sleep(3)
            read_geometry(window, out)
            try:
                out["page"] = window.evaluate_js(
                    "JSON.stringify({inner:[innerWidth,innerHeight],dpr:devicePixelRatio,"
                    "button:document.getElementById('continue').getBoundingClientRect().toJSON()})")
            except Exception as exc:  # the renderer may be absent on a bare image; the form geometry is the measure
                out["page_error"] = repr(exc)
        except Exception as exc:
            out["error"] = repr(exc)
        finally:
            print("MEASURE " + json.dumps(out), flush=True)
            for open_window in list(webview.windows):
                open_window.destroy()

    watchdog = threading.Timer(120, lambda: (print("MEASURE " + json.dumps({**out, "error": "timeout"}), flush=True), sys.exit(3)))
    watchdog.daemon = True
    watchdog.start()
    webview.start(probe)


if __name__ == "__main__":
    main()
