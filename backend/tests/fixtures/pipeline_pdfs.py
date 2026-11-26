"""Generate reusable PDFs that contain only fictional test data."""
import argparse
from pathlib import Path

import pymupdf


def generate_pipeline_pdfs(directory):
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "question-paper.pdf": ["1. What is the derivative of x squared?", "2"],
        "model-answer.pdf": ["Q1. The power rule gives 2x."],
    }
    for index, roll in enumerate(["251081011", "251081068", "251081046", "251081001"], 1):
        files[f"answer-{index}.pdf"] = [f"Roll No: {roll}\nQ1. The derivative is 2x by the power rule."]
    files["mismatch-example.pdf"] = ["Roll No: 999990001\nQ1. This is optional fictional mismatch data."]
    for name, pages in files.items():
        with pymupdf.open() as pdf:
            for text in pages:
                pdf.new_page().insert_text((72, 72), text)
            pdf.save(directory / name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    generate_pipeline_pdfs(parser.parse_args().directory)
