"""What is actually fitted in this box, and what it can actually tell us.

Three ideas, and keeping them apart is the point of the package:

**The registry** (`registry.py`) is a declarative table of every device type the
station supports: which slot it fills, how it connects, what parameters it needs,
and — the field that does the real work — **which telemetry values it can
genuinely source**. Adding support for another weather head is one entry.

**The inventory** (`inventory.py`) is what an installer said is fitted, and
separately what the station actually found. `contract/enrolment.md` §7 is
explicit that these are different facts: the station owns the truth about what
is attached, and the platform reconciles against it. "An Airmar should be on
/dev/ttyUSB0" and "there is an Airmar on /dev/ttyUSB0" are not the same
statement, and a camera that has failed and a camera that was never fitted look
identical in a database and completely different at the site.

**The drivers** turn a device into readings. Where a device provides a value it
is published; where it does not, the value is *absent* rather than zero. A
console showing 0.0 mm of rain during a downpour because no rain gauge exists is
exactly the silent wrongness this project keeps designing against.
"""
