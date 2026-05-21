# Cowork-CC-dispatch

オーケストレーション基盤 — 1 つの AI エージェント（Cowork）が別の AI エージェント（Claude Code）に開発タスクを dispatch し、結果を受け取って `main` に取り込むまでを自動化する。設計の全体像は [`docs/DESIGN.md`](docs/DESIGN.md) を参照。

> Status: v1 実装中（spec_001 — Python スケルトン + CI）

## 必要環境

- Python 3.11+
- (推奨) WSL Ubuntu / Linux / macOS

## セットアップ

```bash
git clone https://github.com/kazikimaguro13/Cowork-CC-dispatch.git
cd Cowork-CC-dispatch
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

動作確認:

```bash
python -m ccd --version   # ccd 0.1.0
ruff check .
pytest -q
```

## レイアウト

```
ccd/                       # import パッケージ（配布名は cowork-cc-dispatch）
  __init__.py              # __version__
  __main__.py              # `python -m ccd` エントリポイント
  cli.py                   # CLI 実装
tests/                     # pytest
docs/DESIGN.md             # 設計の正典（変更しない）
.github/workflows/ci.yml   # ruff + pytest を Python 3.11 / 3.12 で実行
```

## 開発フロー（v1）

spec → dispatch → 実装 → smoke (ruff + pytest) → merge。詳細・スコープ・計測指標は [`docs/DESIGN.md`](docs/DESIGN.md) を参照。

## ライセンス

MIT
