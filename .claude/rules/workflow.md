## Workflow
1. Analyze the request
2. Check existing files (`agent_hello.py`, relevant `tool_*.py`)
3. Propose changes (Plan Mode for complex tasks)
4. Execute changes and run verification
5. If Claude makes a mistake -> fix it, then update `.claude/rules/known-bug-patterns.md`
   (or the relevant skill) with the lesson — see `bug-pattern-archivist`

## AI Instruction Principles (Vibe Coding 2.0)
- **1 session, 1 feature**: 1つの会話に複数の修正/機能を混ぜない。新しい機能・バグ修正は新しいチャットで
- **50-line checkpoints**: 複雑な実装は ~50行ごとに止めてレビューを待つ。一度に大きな塊を生成しない
- **Commit after every working state**: 動いた時点で即コミット。次のタスクへ進む前に
- **Stop and reset when confused**: 進捗のないデバッグループに入ったらコード編集をやめ、平易な言葉で目的を言い直して Plan Mode へ戻る
- **効果的な時間削減は根源的な財産**: 再テスト・再検証のたびに払う固定費を削る変更 (絞り込み実行・並列化・キャッシュ) は、機能追加に見えなくても後回しにしない (`scripts/run_test_sweep.py --filter` 追加の根拠)。ただし削減効果が明確でない先回りの汎用化までは正当化しない

## Plan Mode Protocol
- Complex tasks (3+ files, new features, architectural changes): always start in Plan Mode
- **Verification plan is MANDATORY**: Every plan must include HOW to prove it works
- If implementation goes wrong, **STOP immediately** and return to Plan Mode — never push through
- Never micromanage — give intent, let Claude execute
- テンプレート: `## Goal` / `## Steps` (1.[Step]→[Expected result]) / `## Verification` (`- [ ]` で検証手段) / `## Rollback`

## Output Style: Explanatory + Learning
- 変更するときは常に **why** を説明する (what だけで終わらせない)。非自明なロジックには根拠を、
  新パターン導入時はトレードオフを、バグ修正では真因を先に述べる
- **Learning mode**: 未知の概念を聞かれたら「1段落の要約 + 具体的なコード例 + よくある落とし穴」で答える
- **Decision log**: アーキテクチャ上の決定は Decision → Reason → Alternatives considered で残す

## Visualization Protocol
- 複雑なシステムの説明には **ASCII図** か **Mermaid** を使う (データフロー=シーケンス図 /
  アーキテクチャ=コンポーネント図 / 状態遷移=状態図)。要素が5個を超えたら凡例を付ける
- 網羅的な説明が要るときは **HTML プレゼン**の生成を提案する

## Sub-agent Delegation Protocol

| Agent (`.claude/agents/`) | 使いどころ |
|---|---|
| `verifier` | PR説明文/最終要約/ドキュメントの主張が証跡に基づいているか検査 (コミット/投稿前) |
| `test-writer` | 新規バグ修正・機能に対応するテストを既存作法 (LEVEL 0 encoding, ast静的検査) で追加 |
| `bug-pattern-archivist` | バグ修正後の教訓を CLAUDE.md 本体か該当 skill かを判断して追記 (肥大化防止) |

**委譲する条件**: 3ファイル以上に及ぶ / プロジェクト全域のスキャン (`/techdebt`, 全ファイル grep) /
ドキュメント生成 (README, API docs, CHANGELOG) / テストスイート一式の作成 / 大量データ処理

**Rules**: 重いタスクはプロンプトに `use subagents` を付ける / 各サブエージェントには
ONE focused task だけ渡す / 結果はファイルに書かせる (メインコンテキストに返さない) /
メインはマージ・レビュー・コミットのみ

**Do NOT**: ❌ `CLAUDE.md` や `.env` を編集させる / ❌ 50行未満の変更で起動する /
❌ 3体以上を直列に連鎖させる (並列化する) / ❌ 出力をレビューせずにコミットする
