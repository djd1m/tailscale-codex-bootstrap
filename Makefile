.PHONY: check test
check:
	python3 -m compileall -q src tests
	bash -n bin/bootstrap
	PYTHONPATH=src python3 -m unittest discover -s tests -v
test: check
