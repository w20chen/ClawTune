"""Summarize frozen offline shell durations, retaining abstentions as misses."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def read(path: Path) -> bytes:
    return path.read_bytes() if path.is_file() else gzip.decompress(path.with_suffix(path.suffix + ".gz").read_bytes())


def summarize(rows: list[dict], bucket_count: int) -> dict:
    matrix = [[0] * (bucket_count + 1) for _ in range(bucket_count)]
    for row in rows:
        predicted = row["predicted_bucket"]
        matrix[row["actual_bucket"]][bucket_count if predicted is None else predicted] += 1
    buckets = []
    for index in range(bucket_count):
        support = sum(matrix[index])
        unavailable = matrix[index][-1]
        predicted_count = sum(row[index] for row in matrix)
        tp = matrix[index][index]
        buckets.append({
            "bucket": index, "support_all": support,
            "support_available": support - unavailable, "unavailable": unavailable,
            "predicted_count": predicted_count, "true_positive": tp,
            "false_positive": predicted_count - tp, "false_negative_all": support - tp,
            "precision": tp / predicted_count if predicted_count else None,
            "recall_all": tp / support if support else None,
            "recall_available": tp / (support - unavailable) if support > unavailable else None,
        })
    predicted = sum(row["support_available"] for row in buckets)
    correct = sum(row["true_positive"] for row in buckets)
    all_recalls = [row["recall_all"] for row in buckets if row["recall_all"] is not None]
    available_recalls = [row["recall_available"] for row in buckets if row["recall_available"] is not None]
    return {
        "eligible": len(rows), "predicted": predicted, "correct": correct,
        "coverage": predicted / len(rows) if rows else None,
        "accuracy_available": correct / predicted if predicted else None,
        "micro_recall_all": correct / len(rows) if rows else None,
        "macro_recall_all": statistics.mean(all_recalls) if all_recalls else None,
        "macro_recall_available": statistics.mean(available_recalls) if available_recalls else None,
        "per_bucket": buckets, "confusion_matrix_with_abstention_last": matrix,
    }


def compare(run: Path) -> dict:
    report = json.loads(read(run / "report.json"))
    split = json.loads(read(run / "split.json"))
    grouped = defaultdict(list)
    for line in read(run / "model-predictions.jsonl").splitlines():
        row = json.loads(line)
        if row["target"] == "duration_ms":
            grouped[row["model"]].append(row)
    if not grouped:
        raise ValueError("no duration predictions")
    reference = next(iter(grouped.values()))
    identity = ("task", "benchmark", "repo", "tool", "actual", "actual_bucket")
    for name, rows in grouped.items():
        if len(rows) != len(reference) or any(
            any(row[field] != ref[field] for field in identity)
            for row, ref in zip(rows, reference)
        ):
            raise ValueError(f"unaligned evaluation rows: {name}")
    edges = report["bucket_edges"]["duration_ms"]
    count = len(edges) + 1
    # Verify the original report before narrowing older mixed-tool exports.
    for name, rows in grouped.items():
        value = summarize(rows, count)
        metrics = [row["bucket_metrics"] for row in report["models"][name]["metrics"]
                   if row["target"] == "duration_ms"]
        if len(metrics) != 1:
            raise ValueError("summarize one benchmark at a time")
        if (value["eligible"] != metrics[0]["eligible"]
                or value["predicted"] != metrics[0]["predicted"]
                or value["accuracy_available"] != metrics[0]["accuracy"]):
            raise ValueError(f"summary disagrees with offline report: {name}")
    grouped = {name: [row for row in rows if row["tool"] in {"exec", "terminal_exec"}]
               for name, rows in grouped.items()}
    reference = next(iter(grouped.values()))
    if not reference:
        raise ValueError("no shell duration predictions")
    common = [index for index in range(len(reference))
              if all(rows[index]["predicted_bucket"] is not None for rows in grouped.values())]
    models = {name: summarize(rows, count) for name, rows in grouped.items()}
    for name, rows in grouped.items():
        models[name]["unavailable_reasons"] = dict(Counter(
            row.get("unavailable_reason") or "unspecified" for row in rows
            if row["predicted_bucket"] is None
        ))
    return {
        "target": "duration_ms", "bucket_edges_ms": edges, "scope": "shell_calls",
        "classification": "argmax bucket probability; ties choose lower bucket",
        "accuracy_definition": "correct / available predictions",
        "recall_all_definition": "per-bucket TP / all actual members; unavailable counts as false negative",
        "macro_recall_all_definition": "unweighted mean recall over buckets with actual support",
        "micro_recall_all_definition": "correct / all labeled calls",
        "train_tasks": report["train_tasks"], "test_tasks": report["test_tasks"],
        "test_updates": report["test_updates"], "seed": split["seed"],
        "train_fraction": split["train_fraction"], "split_sha256": report["split_sha256"],
        "trace_files": sum(len(task["files"]) for task in split["tasks"].values()),
        "unique_tasks": len(split["tasks"]), "excluded_files": split.get("excluded_files", []),
        "evaluated_tasks": len({row["task"] for row in reference}),
        "models": models, "common_count": len(common),
        "common_tasks": len({reference[index]["task"] for index in common}),
        "common_models": {name: summarize([rows[index] for index in common], count)
                          for name, rows in grouped.items()},
        "shell_eligible": len(reference), "shell_models": models,
        "artifacts_sha256": {name: hashlib.sha256(read(run / name)).hexdigest()
                             for name in ("report.json", "split.json", "model-predictions.jsonl")},
    }


def render(result: dict) -> str:
    def pct(value):
        return "—" if value is None else f"{100 * value:.2f}%"

    def table(headers, rows):
        return "<table><thead><tr>" + "".join(f"<th>{html.escape(str(v))}</th>" for v in headers) + "</tr></thead><tbody>" + "".join(
            "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>" for row in rows
        ) + "</tbody></table>"

    summary = table(["方法", "可预测 / 有标签", "覆盖率", "准确率（可预测）", "宏召回（全部）", "微召回（全部）", "共同集合准确率"], [
        [name, f"{row['predicted']} / {row['eligible']}", pct(row["coverage"]), pct(row["accuracy_available"]),
         pct(row["macro_recall_all"]), pct(row["micro_recall_all"]), pct(result["common_models"][name]["accuracy_available"])]
        for name, row in result["models"].items()
    ])
    boundaries = [0, *result["bucket_edges_ms"], "∞"]
    details = ""
    for name, row in result["shell_models"].items():
        details += f"<h2>{html.escape(name)}</h2>" + table(
            ["桶（ms）", "实际样本", "不可预测", "预测到此桶", "正确", "精确率", "召回（全部）", "召回（可预测）"],
            [[f"[{boundaries[b['bucket']]}, {boundaries[b['bucket']+1]})", b["support_all"], b["unavailable"],
              b["predicted_count"], b["true_positive"], pct(b["precision"]), pct(b["recall_all"]), pct(b["recall_available"])]
             for b in row["per_bucket"]],
        )
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>KB 离线分桶评估</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:1200px;margin:36px auto;padding:0 20px;color:#19242f}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;margin:20px 0}}th,td{{border:1px solid #d6dde4;padding:8px;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#edf2f6}}p{{max-width:1000px}}</style>
<h1>KB 离线 shell 耗时分桶评估</h1>
<p>{result['trace_files']} 个 trace，{result['unique_tasks']} 个任务；训练 {result['train_tasks']}，留出测试 {result['test_tasks']}。
seed={result['seed']}，训练比例参数 {result['train_fraction']}，测试更新 {result['test_updates']}。所有 Lattice 变体共享训练统计。
lattice 表示 shrinkage。</p>
<p>预测桶取概率最大值，平局选较低桶。准确率只统计已预测样本；全部样本召回把不可预测计为漏报。
宏召回对有实际样本的桶等权平均，微召回为正确数除以全部有标签调用数。
共同集合包含 {result['common_count']} 次调用、{result['common_tasks']} 个任务，仅反映所有方法都能预测的子集。</p>
<h2>有标签的 shell 调用</h2><p>全部指标仅统计同一批 {result['shell_eligible']} 次 exec / terminal_exec 调用。</p>{summary}
{details}<p>完整混淆矩阵和指标定义见 summary.json；原始记录见输入实验的 model-predictions.jsonl（或其 gzip 文件）。</p></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = compare(args.run)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "report.html").write_text(render(result), encoding="utf-8")
    print(json.dumps({name: {key: value for key, value in row.items() if not isinstance(value, list)}
                      for name, row in result["models"].items()}, indent=2))


if __name__ == "__main__":
    main()
