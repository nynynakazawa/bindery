# 暗号化同期の設計

## 範囲

`bindery sync` は Vault のファイルを暗号化し、Vault の外に置いた Git リポジトリで運びます。
Vault には `.git` を作りません。同期用リポジトリは
`<state-dir>/sync/<vault-key>/repo` です。Git は履歴と運搬を担当しますが、Markdown の本文を
マージしません。

守る対象は、リモート、リモートのバックアップ、通信経路から見た本文とパス名です。改ざん、
世代の巻き戻し、履歴の断絶も受理しません。平文 Vault と鍵を持つ端末の侵害は範囲外です。

## Git tree と暗号フォーマット

Git tree に入る名前は次の3種類だけです。

```text
format.json
manifest.enc
objects/<object-id>.enc
```

`format.json` は次の形式です。バージョンと暗号方式名以外は記録しません。

```json
{"version": 1, "encryption": "AES-256-GCM/HKDF-SHA256"}
```

マスター鍵は `os.urandom(32)` で作ります。HKDF-SHA256 から用途ごとに次を導出します。

| 用途 | 導出情報 | 長さ |
| --- | --- | ---: |
| AES-256-GCM | `bindery-sync/enc` | 32 bytes |
| object ID の HMAC | `bindery-sync/name` | 32 bytes |
| 鍵 ID | `bindery-sync/key-id` | 8 bytes |

object ID は `HMAC-SHA256(name-key, 相対パス)` の先頭32文字です。パスを暗号化 manifest の外へ
出さず、同じパスの更新だけが同じ object ID を使います。

object の blob は次のバイト列です。

```text
BDY1 | key-id(8) | nonce(12) | ciphertext + GCM tag
```

nonce は毎回 `os.urandom(12)` で作ります。object の AAD は、形式バージョン、用途、sync ID、
object ID を長さ付きで連結した値です。

manifest の blob には世代を認証対象の平文ヘッダーとして追加します。

```text
BDY1 | key-id(8) | generation(8, big-endian) | nonce(12) | ciphertext + GCM tag
```

manifest の AAD にはこのヘッダー全体、用途、sync ID、`manifest`、世代を含めます。復号後の JSON
にも同じ世代を持たせ、ヘッダーと本文が違えば `blocked` にします。manifest の各項目は相対パス、
object ID、平文 SHA-256、サイズ、機械ラベルです。復号後に object のハッシュとサイズを照合します。

manifest の `generation` は増加し、`previous_hash` は直前の manifest の SHA-256 です。state は最後に
受理した世代とハッシュを持ちます。受信した manifest から `previous_hash` をたどり、受理済み
チェックポイントまで世代を一つずつ戻せない場合は、Git の親関係にかかわらず受理しません。鍵ごとの
暗号化回数が `2^32` に達したら rekey を促します。

コミットの author、committer、日時は固定値です。`bindery <bindery@invalid>`、1970-01-01、
メッセージ `sync` を使います。それでもコミット数、順序、ファイル数、暗号文サイズ、更新頻度は
漏れます。

## 鍵の保管と受け渡し

macOS では `security` コマンドを使い、service `bindery-sync` と sync ID を Keychain の account
にします。登録時の鍵は `security -i` の標準入力へ渡し、取得は `find-generic-password -w` の
標準出力から行います。macOS 以外では state dir の 0600 ファイルを使います。

鍵は Git、Vault、ログ、state JSON、プロセス引数に書きません。`sync pair <ssh-host>` は
`StrictHostKeyChecking=yes` の SSH を起動し、鍵と信頼チェックポイントを JSON で標準入力へ渡します。
相手機に同じ sync ID の別鍵があれば拒否し、`--replace` のときだけ置き換えます。`sync key export`
の出力は復旧用なので、通常のログへ保存しません。

## 同期の状態遷移

1. Vault を走査し、シンボリックリンク、境界外、`.obsidian/workspace*.json`、`.obsidian/cache`、
   `.DS_Store`、`.trash`、`*.tmp`、索引の exclude 対象を除外する。
2. `.md` のローカル変更を UTF-8 として読み、`redact.contains_secret` で検査する。秘密を含む
   ファイルは保留し、暗号化対象から外す。短い値や未知の形式は検出できない。
3. Git remote を fetch し、manifest の認証、世代、ハッシュ連鎖、object の SHA-256 とサイズを検証する。
   remote manifest に現在の境界外パスが残っていれば、追跡解除を自動化せず `blocked` にする。
4. 前回の base、local、remote をパス単位で比較する。初回は片側だけの追加を削除と扱わない。
   変更対削除は変更を残し、両側が変更した Markdown は内容 SHA-256 の辞書順で大きい版を元のパスに
   置き、もう一方を `.conflict-<sha12>.md` に書く。生成済みの衝突コピー自身も 3-way の対象とし、
   同じ計画でそのパスを更新する場合は、既存内容をそのコピーの `.conflict-<sha12>.md` に退避する。
   Markdown 以外の内容競合は停止する。
5. 全書き込み先を検証する。絶対パス、`..`、NUL、`.git`、Vault 外へ出る親 symlink、NFC と casefold
   で衝突するパス、ファイルとディレクトリの衝突があれば、Vault を変更しない。
6. 計画を state に保存する。計画は平文本文を持たず、書き込む暗号化 object の参照とハッシュを持つ。
   Vault ロック下で各ファイルの現在 SHA-256 が期待値と一致する場合だけ atomic write する。途中で一つでも
   不一致なら、その時点までの書き込みを残し、commit、push、base 更新をせず計画を破棄して同じ実行内で
   最大3回走査から再開する。Markdown の衝突では衝突コピーを元パスより先に書く。
7. 暗号文 tree を作り、local HEAD と remote HEAD を親にした commit-tree を作る。push が
   non-fast-forward なら、走査から最大3回やり直す。結果不明の通信失敗は次回 fetch で自分の
   commit が remote にあれば成功として扱える状態にする。
8. push 成功後に base、受理世代、索引追随の世代を atomic 更新する。変更パスを強制再索引し、
   embedding と ANN を更新する。索引更新に失敗した場合、Git は `ok`、index は `error` と分けて記録する。

MCP サーバーは state の適用世代をツール呼び出し前に確認し、世代が変われば再起動なしで増分再索引します。
Vault への通常の書き込みも同じ Vault ロックを、Vault ロック、ファイルロックの順に取ります。

## 状態の分類

| 状態 | 意味 | 終了コード |
| --- | --- | ---: |
| `git: ok` | 暗号文の commit と push が完了した | 0 |
| `git: error` | 通信、Git、鍵取得などの一時的な失敗 | 1 |
| `git: blocked` | 改ざん、巻き戻し、パス攻撃、競合など人手の判断が要る | 2 |
| `index: ok` | Vault の変更を索引へ反映した | 0 |
| `index: error` | Git は完了したが索引更新に失敗した | 1 |
| `index: skipped` | embedding backend がないなど、Git 後に省略した | 0 |

秘密を含むローカルファイルは `held_secrets` に数えます。これは他の変更を同期できるため、
それだけでは終了コード2にしません。リモートから復号した内容に秘密らしき文字列があっても、
警告として記録して適用します。

## 復旧表

| 停止位置 | 残る状態 | 次回の動作 |
| --- | --- | --- |
| 走査から計画保存まで | Vault は変更しない | 最初から走査し直す |
| Vault 適用の途中 | それまでに計画どおり書いたファイルが残る。commit、push、base は未更新 | 計画を捨てて走査し直す。残った内容は local の変更として同じ内容に収束する |
| 暗号文 tree 作成または commit 中 | state の base は未更新 | local HEAD と remote HEAD を fetch し、同じ暗号文を再利用して commit を作る |
| push 中にタイムアウト | push の成否が不明 | 次回 fetch で remote HEAD が自分の commit なら成功として扱う。無ければ再試行する |
| push 後、base 更新前 | remote と Vault は更新済み | remote manifest を再取得し、base と受理世代を再保存する |
| 索引更新中 | Git と Vault は更新済み、index は `error` の可能性がある | MCP サーバーの次の呼び出し、または `bindery index` で再索引する |
| `blocked` | Vault と暗号文は勝手に戻さない | `sync status` の理由と対象パスを確認し、競合解決、鍵の再ペアリング、手動復旧を選ぶ |

`rekey --write` は新しい鍵を作る前に fetch し、通常の同期（統合、適用、push）を成功させます。
ローカルの同期 HEAD とリモート HEAD が一致したことを確認して新しい根を作り、観測した HEAD を
lease に指定して force push します。根の作成中に別端末が push して HEAD が変わると push を拒否し、
旧鍵を残したまま中止します。
旧鍵と旧ローカルリポジトリは `master.key.previous-*` と `repo.previous-*` に退避し、削除しません。
旧 Git オブジェクトは reflog、未到達オブジェクト、バックアップに残り得るため、旧鍵を失効させる操作では
ありません。

