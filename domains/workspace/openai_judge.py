"""Chat Completions transport for Harvey LAB's pinned rubric evaluator.

The prompt files and score_rubric remain in the external Harvey LAB checkout.
The filename-matching prompt follows harveyai/harvey-labs@1da4750,
evaluation/scoring.py, replacing its separate Anthropic request.
"""

from __future__ import annotations

import json
from pathlib import Path

from rrsi.env import model_name, require_openai_env
from rrsi.llm import generate


class OpenAIJudge:
    def __init__(self, prompts_dir: Path):
        require_openai_env()
        self.model = model_name()
        self.prompts_dir = prompts_dir

    def evaluate(self, prompt_template: str, variables: dict,
                 temperature: float = 0.0, _retries: int = 2) -> dict:
        prompt = prompt_template.format(**variables)
        last_error = None
        for _ in range(_retries):
            text = generate(prompt, json_only=True, max_tokens=16384,
                            temperature=temperature)
            try:
                result = json.loads(text)
                if (not isinstance(result, dict)
                        or result.get("verdict") not in {"pass", "fail"}
                        or not isinstance(result.get("reasoning"), str)):
                    raise ValueError("Judge must return verdict=pass|fail and reasoning")
                return result
            except (ValueError, TypeError) as e:
                last_error = e
        raise ValueError(f"Judge returned an invalid verdict after {_retries} attempts") from last_error

    def evaluate_from_file(self, prompt_name: str, variables: dict) -> dict:
        template = (self.prompts_dir / f"{prompt_name}.txt").read_text(encoding="utf-8")
        return self.evaluate(template, variables)


def match_deliverables(unresolved: dict, available_files: list[str],
                       output_dir: Path, read_file) -> dict:
    previews = []
    for filename in available_files:
        path = output_dir / filename
        content = read_file(path)[:500] if path.exists() else "(file not found)"
        previews.append(f"Filename: {filename}\nPreview: {content}\n")
    descriptions = [f"Deliverable key: {key}\nExpected filename: {expected}"
                    for key, expected in unresolved.items()]
    prompt = ("Match each unresolved deliverable to the most likely output file.\n\n"
              "## Unresolved Deliverables\n" + "\n".join(descriptions)
              + "\n\n## Available Output Files\n" + "\n".join(previews)
              + "\n\nFor each deliverable, provide the matching filename from the "
              "available files, or null if no file matches.\n"
              "Return a JSON object whose keys are the deliverable keys.")
    try:
        result = json.loads(generate(prompt, json_only=True, max_tokens=1024, temperature=0.0))
        if not isinstance(result, dict):
            return {}
        return {key: value for key, value in result.items()
                if key in unresolved and (value is None or value in available_files)}
    except Exception:  # Match upstream: unresolved filenames remain unmatched on API failure.
        return {}
