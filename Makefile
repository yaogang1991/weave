.PHONY: ui-dev ui-build ui-start ui-test ui-clean

ui-dev:
	cd weave_ui/frontend && npm run dev

ui-build:
	cd weave_ui/frontend && npm run build

ui-start:
	python main.py viz

ui-test:
	python -m pytest tests/test_weave_ui_api.py tests/test_weave_ui_*.py -v

ui-clean:
	rm -rf weave_ui/static/* weave_ui/frontend/node_modules weave_ui/frontend/dist
