# LLM フォールバック（ローカル Ternary Bonsai 2 27B）の設計

最終目標「すべての操作を音声で行えるようにする」に向けて、Jev だけで判断できない発話だけを
LLM に解釈させるためのプロンプトと、アプリへの組み込み方を定める。

## 基本方針

- **判定の中心はあくまで Jev**。高速経路（キーワード照合）→ タスクひな形（`tasks.py`）→ 学習した言い方
  （`learner.py`）→ Jev、の順で済むものはそのまま。LLM は「Jev で決まらなかったもの」の
  フォールバックとして、**1 発話につき最大 1 回**だけ呼ぶ。
- 2026-09-22 の拡張で、高速経路（`engines/keyword.py`）とタスクひな形（`tasks.py`）が扱える言い方が大幅に
  増えた（ブラウザ・Windows のショートカット約 45 種、`snap_window`、サイト 25 種、特別なフォルダ 7 種、
  続きの指示は `followup.py` が即判定）。**定型の言い方は Jev にも LLM にも行かない**。
- **LLM は既定でローカル**（この PC の `llama-server` ＋ Ternary Bonsai 2 27B）。発話が外部に出ず、料金も
  かからず、実測 1〜2 秒で返る。`config.yaml` の `backend: opencode` にすれば従来どおり opencode 経由
  （DeepSeek など）も使えるが、常用は想定していない。
- **一度解釈できた言い方は `learner.py` が覚える**ので、同じ言い方で LLM（Jev も）に二度目を聞かない。
  `logs/decisions-*.jsonl` の `engine` が `llm(bonsai)` の割合で「LLM に頼っている度合い」を監視する。
- LLM は**判断して JSON を返すだけ**。ファイル操作・コマンド実行などのツールは一切持たせない
  （opencode 経由のときは permission で全ツール拒否）。実行は常に既存の executor・確認フローを通る。
- LLM の結果は既存の Decision と同じ形式に載せ替え、**しきい値（実行 0.80 / 確認 0.50）もそのまま使う**。
  LLM 専用の特別扱いは作らない。`logs/decisions-*.jsonl` の engine を `llm(bonsai)` と記録し、
  LLM 出番が増えていないか（目安: 全命令の 10% 未満）をログで監視する。

## 使うモデル

| 項目 | 値 |
|---|---|
| モデル | **Ternary Bonsai 2 27B**（PrismML、Qwen 系 27B の三値蒸留・Apache 2.0） |
| 実行 | `llama-server`（PrismML フォーク・CUDA）を 4080S に常駐。重みは `S:\models\Ternary-Bonsai-2-27B\` |
| エンドポイント | `http://127.0.0.1:8080/v1`（OpenAI 互換。**ループバック以外は設定しても使わない**） |
| 速度 | 約 60〜70 t/s、1 発話の判定で 1〜2 秒（思考オフ） |
| temperature | 0.0（判定用途のためリクエストで固定。`chat_template_kwargs: enable_thinking=false`） |
| opencode から使う場合 | `llama-cpp/bonsai2-27b`（`~/.config/opencode/opencode.json` に登録済み） |

## プロンプトの所在

**`.opencode/agents/voicectl-fallback.md`** — プロンプト本体。
opencode のエージェント定義ファイル（本文がシステムプロンプトになる）として置いてあり、
フロントマターでモデル・temperature・ツール全拒否を指定済み。
**ローカル backend でも同じファイルの本文をシステムプロンプトとして使う**（フロントマターは読み飛ばす）。
プロンプトの調整はこのファイルを編集するだけでよく、アプリ側は無変更で反映される。

## いつ LLM を呼ぶか（呼び出し条件）

既存パイプライン（`controller._handle_text`）のどこに差し込むか:

| # | 条件 | mode（入力のヒント） | 期待する結果 |
|---|---|---|---|
| 1 | Jev の確信度が確認しきい値 0.50 未満、かつ `is_pc_command >= 0.5` | `interpret` | 単一命令への解釈 |
| 2 | Jev が `unknown`、かつ `is_pc_command >= 0.5`（JevAgent に丸投げする前に 1 回） | `interpret` | 単一命令への解釈 |
| 3 | `type_text` と判定されたが `textparse.extract_text` で文字列が取れない／表記変換が必要 | `text` | 入力する確定文字列 |
| 4 | Jev が `unknown` で、発話が長い（12 文字以上）。`is_pc_command` が低くても渡す | `interpret` | 長い複文の解釈・分解 |
| 5 | Jev API 呼び出し失敗、かつキーワードフォールバックも `unknown` | `interpret` | 単一命令への解釈 |

- **長い発話を落とさない**のが条件 4。Jev は長い複文で `is_pc_command` を低く出して `unknown` を返すことが
  あり、以前は「PC の命令ではない」として聞き返しになっていた（`_llm_worth_trying`）。
- 呼び出しに失敗しても**永久には無効化しない**。3 回続けて失敗したら `cooldown_sec`（既定 90 秒）だけ休み、
  そのあと自動で再開する（`llama-server` を後から起動した場合でも使えるようになる）。
- JevAgent（1 手ずつ Jev に選ばせる多段実行）は、条件 2 を LLM が解決できなかった場合の次段として残す。
- どの条件でも **LLM の返した confidence が 0.50 未満なら通常どおり聞き返す**。LLM は最後の砦ではなく
  「もう一段賢い解釈者」。
- LLM が解釈できた言い方（plan を含む）は `learner.remember()` で覚えるので、次回は LLM を通らない。

## 入出力契約

プロンプト本体（`.opencode/agents/voicectl-fallback.md`）に全文を定義済み。要点:

- **入力**: JSON 1 つ。`mode` / `utterance` / `today` / `active_window` / `focused_control` / `cursor` /
  `previous_command` / `recent_commands` / `session` / `visible_elements` / `candidates`（element / menu / app /
  window / command の ID → ラベル。Jev に渡すものと同じ候補を使い回す）/ `facts`（`best_element_matches` 等、
  `jev.build_state` と同じものを使い回す）
  - `session` は controller が持つ直前の文脈（`last_app` / `last_site` / `last_query` / `last_url` /
    `last_location` / `last_target`）。「さっきのアプリ」「さっきのページ」の解決に使う（空の値は送らない）
- **出力**: JSON 1 つ。`is_pc_command` / `action` / `params` / `text` / `plan` / `clarify` / `confidence` / `reason`。
  action・params の id は `schema.py`（ACTIONS / DIRECTIONS / SNAPS / …）と完全に同じものを使う。
  plan ステップのみ `open_site` / `search_site` / `open_location`（`tasks.py` の SITES / LOCATIONS が対応）を
  追加で使える。
- **アプリ側の検証（必須）**: action は schema.py に存在するか / params の ID は入力 candidates に存在するか
  （`snap` は `schema.SNAPS` のキーか、plan の `site` / `location` は `tasks.SITES` / `tasks.LOCATIONS` のキーか）/
  `text` は長さ上限（例: 200 字）以内か / `plan` は 5 ステップ以内か。1 つでも違えば破棄して聞き返す。

## 呼び出し方（実装済み: `voicectl/engines/llm.py`）

既定（`backend: local`）は `LLMFallback._run_local()` が `POST {base_url}/chat/completions` を叩く。

- 宛先は起動時に `local_endpoint()` で検証する。**`http`/`https` かつループバック（127.0.0.1 / localhost / ::1）
  だけ**を許可し、それ以外の設定は警告して local backend を使わない（発話と画面の候補を外部に送らないため）。
- リダイレクトは追わない（`_NoRedirect`）。応答は最大 1 MB までしか読まない。
- `temperature: 0.0`、`chat_template_kwargs: {"enable_thinking": false}`（思考オフで 1〜2 秒）、
  出力上限 `max_tokens`（既定 512）。

`backend: opencode` を選んだときは従来どおり subprocess で:

```
opencode run --agent voicectl-fallback --model <model> \
  --pure --format json --title voicectl --dir S:\voice-control  "<入力JSON>"
```

- 出力は `--format json` のイベント列。`type: "text"` の `part.text` を連結したものが応答本体。
- **npm の opencode.CMD シムは Python の subprocess から直接実行すると出力が空になる**
  （2026-09-22 実測）。`_resolve_command` がシムの呼ぶ本体
  `…\npm\node_modules\opencode-ai\bin\opencode.exe` を直接使うよう解決する。
- `controller._act` が Jev で決まらなかった発話（unknown・確信度 0.50 未満）のときだけ 1 回呼ぶ。
  結果は clarify（聞き返し文の表示）／ plan（複数手順。取り消しにくい操作を含む場合は全体を一度確認）／
  通常の Decision（既存の確認→実行の流れに乗る）のどれか。JevAgent はその次の段として残る。
- plan の実行（`controller._exec_plan`）は **1 手が失敗しても残りを続けて実行**し、最後に結果をまとめて知らせる。
  失敗した位置は覚えていて、「続き」と言えばその手順から再開する。この方針はプロンプトにも書いてあり、
  LLM が「失敗を恐れて手順を省く」ことを防いでいる。

## config.yaml（実装済み）

```yaml
decision:
  llm_fallback:
    enabled: true
    backend: local               # local = llama-server（OpenAI 互換） / opencode
    model: bonsai2-27b
    agent: voicectl-fallback
    local:
      base_url: http://127.0.0.1:8080/v1   # この PC 以外は指定できない
      model: bonsai2-27b
      timeout_sec: 8.0
      max_tokens: 512
    timeout_sec: 8.0             # backend: opencode のときのタイムアウト
    cooldown_sec: 90             # 続けて失敗したら休んでから自動で再開する
    max_plan_steps: 5
    max_text_len: 200
```

## 安全性

- ローカル backend の宛先はループバックのみ（上記）。発話がネットに出る経路がない。
- opencode 経由のときは LLM にツールを全拒否（permission `"*": deny`）で、実行能力を持たせない。
- `type_text` / `window_control: close` / `alt_f4` / `close_tab` は既存の `always_confirm` により
  LLM 経由でも必ず確認が入る。plan 内のこれらのステップも全体を一度確認する。
- 候補 ID の捏造はアプリ側検証で落とす（上記）。
- LLM からの応答がパースできない・タイムアウトした場合は、何も実行せず通常の聞き返しに落とす。
- 学習（`learner.py`）は実行して**成功した**結果だけを信頼し、失敗が成功を上回った言い方は忘れる。

## 検証

- プロンプト単体（opencode 経由のとき）:

  ```
  opencode run --agent voicectl-fallback --model llama-cpp/bonsai2-27b --pure \
    '{"mode":"text","utterance":"ギットハブのユーザー名って打って","today":"2026-09-22 (火)",
      "active_window":{"title":"メモ帳","process":"notepad.exe"},"cursor":{"x":100,"y":200},
      "previous_command":null,"recent_commands":[],"visible_elements":[],
      "candidates":{},"facts":{"katakana_english":{"ギットハブ":"GitHub"}}}'
  ```

  → JSON 1 行（`action: "type_text"`, `text: "GitHubのユーザー名"` 等）が返れば OK。
- ローカル backend の確認: `llama-server` を起動した状態で
  `.venv\Scripts\python.exe tests\smoke_commands.py`（LLM には触れない）と
  `curl http://127.0.0.1:8080/v1/models`（サーバの生存確認）。
- `tests/test_logic.py` のケース: 出力 JSON → Decision 変換（正常系・不正 ID・フェンス付き・JSON 破損）、
  plan の各ステップ→ executor へのディスパッチ、呼び出し条件の判定（`test_llm_gate_takes_long_utterance`）、
  冷却からの復帰（`test_llm_cooldown_recovers`）、宛先の制限（`test_llm_local_endpoint_only_loopback`）、
  ローカル応答の採用（`test_llm_local_answer_is_used`）。

## ロードマップ

1. ~~`voicectl/engines/llm.py`: 呼び出し・JSON パース・Decision 変換・検証~~ ✅ 実装済み
2. ~~`controller.py`: 呼び出し条件の差し込み（JevAgent の前段）~~ ✅ 実装済み
3. ~~複合命令検出~~ ✅ `_split_chain` の拡張で読点なしの複文も分割（LLM の plan と二重の安全網）
4. ~~plan 実行器~~ ✅ 各ステップを既存 executor に 1 手ずつ投入（危険な手順を含む plan は全体を一度確認）
5. ~~**ローカル LLM を既定に**（Ternary Bonsai 2 27B / llama-server）＋宛先の制限・冷却付き再試行~~ ✅ 2026-09-22
6. ~~**学習**（`learner.py`）: LLM や確認で解釈できた言い方を覚え、次からは Jev も LLM も通さない~~ ✅ 2026-09-22
7. `logs/decisions-*.jsonl` で LLM 出現率と精度を観察し、Jev 側の選択肢・instructions を育てて
   LLM 出番を減らす（基本方針「殆どを Jev で」の繰り返し）。`state/learned_suggestions.md` の候補を
   `keyword.py` / `profiles/*.yaml` へ移す作業がこれに当たる
8. （任意）JevAgent の初手に LLM の plan を与えて手数を削減
