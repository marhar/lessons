.PHONY: build test test-py test-live

build:
	cmake -S cpp -B cpp/build
	cmake --build cpp/build

# both implementations against the fake VictoriaMetrics
test: build
	pytest

# just server.py, no C++ build needed
test-py:
	pytest --impl=py

# server.py and the C++ server must agree against a real VictoriaMetrics
test-live: build
	pytest -m live
