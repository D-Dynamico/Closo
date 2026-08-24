.PHONY: test install run generate

install:
	pip install -r requirements.txt

test:
	pytest

run:
	streamlit run app/streamlit_app.py

# Regenerate the frozen demo dataset (Stage 2)
generate:
	python -m closo.generator --seed 42 --out data/generated/demo
