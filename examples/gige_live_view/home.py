"""HOME task: nothing moves in this example, there is only a camera."""


def home(devices, context=None):
    if context is not None:
        with context.step("Nothing to home"):
            pass
