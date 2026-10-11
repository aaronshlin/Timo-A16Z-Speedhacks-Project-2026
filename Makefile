# new file, no collision with in-flight app/* work
.PHONY: demo demo-live qa call status logs stop
PYTHON ?= python3
demo:
	$(PYTHON) scripts/demo_ctl.py start --mode dry
demo-live:
	$(PYTHON) scripts/demo_ctl.py start --mode live
qa:
	$(PYTHON) scripts/demo_qa.py
call:
	$(PYTHON) scripts/demo_ctl.py call --once
status:
	$(PYTHON) scripts/demo_ctl.py status
logs:
	$(PYTHON) scripts/demo_ctl.py logs
stop:
	$(PYTHON) scripts/demo_ctl.py stop
