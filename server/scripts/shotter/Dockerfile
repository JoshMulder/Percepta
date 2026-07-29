# A browser that can actually open the console.
#
# The extension normally used for screenshots waits for `document_idle`, and a
# live dashboard never reaches it - telemetry, map redraws and video frames keep
# the page busy indefinitely. Playwright does not wait for idle, but its
# Chromium needs system libraries this host cannot install without root, so it
# runs in here instead.
#
# Microsoft's image ships the browsers and not the Python package, hence the
# pip install. Built once; screenshots are then a second or two.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir playwright==1.61.0
ENTRYPOINT ["python3"]
