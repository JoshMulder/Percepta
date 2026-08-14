"""Alerts that outlive the browser that first saw them.

`rules` decides what becomes an alert; `store` does the opening, acknowledging
and closing. The engine that drives them lives one level up in
services/alert_engine.py, because it runs in the ingest leader and this package
is meant to be importable from an API request without dragging that in.
"""
