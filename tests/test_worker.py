import threading

from PIL import Image

from inkypull.worker import DisplayWorker


class SlowDisplay:
    resolution = (10, 10)

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.shown = []

    def show(self, image):
        self.started.set()
        self.release.wait(5)
        self.shown.append(image)


def _img(colour):
    return Image.new("RGB", (10, 10), colour)


def test_submit_during_refresh_keeps_only_newest():
    display = SlowDisplay()
    worker = DisplayWorker(display)
    first, second, third = _img("red"), _img("green"), _img("blue")
    done = []
    worker.submit(first, lambda e: done.append("first"))
    display.started.wait(5)
    worker.submit(second, lambda e: done.append("second"))
    worker.submit(third, lambda e: done.append("third"))
    display.release.set()
    assert worker.wait_idle(5)
    worker.close(5)
    assert display.shown == [first, third]
    assert done == ["first", "third"]


def test_display_errors_reach_callback_and_worker_survives():
    class Flaky:
        resolution = (10, 10)
        calls = 0

        def show(self, image):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise OSError("SPI timeout")

    worker = DisplayWorker(Flaky())
    errors = []
    worker.submit(_img("red"), errors.append)
    worker.wait_idle(5)
    worker.submit(_img("red"), errors.append)
    worker.wait_idle(5)
    worker.close(5)
    assert isinstance(errors[0], OSError) and errors[1] is None
