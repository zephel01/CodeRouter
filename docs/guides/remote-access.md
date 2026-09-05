# リモートアクセスガイド — 別の PC から CodeRouter に安全に繋ぐ

> English: [`remote-access.en.md`](./remote-access.en.md)

CodeRouter を動かしている機体とは**別の PC**(同じ LAN、あるいは外出先)から dashboard やチャット入口を使いたい、というときのガイドです。結論を先に: **選択肢は 4 つあり、迷ったら SSH トンネル(個人)か Tailscale(複数端末)**です。`--host 0.0.0.0` での素の LAN 公開は、信頼できるネットワーク以外では推奨しません。

---

## まず前提 — CodeRouter の信頼境界

2 つの事実を押さえてください。

1. **チャット入口(`/v1/messages` / `/v1/chat/completions`)と dashboard に認証はありません。** ネットワーク的に届く人は誰でも、あなたのモデルで推論できます
2. **`CODEROUTER_ALLOWED_HOSTS` は認証ではありません。** これは Host ヘッダ検証(ブラウザ経由の DNS リバインディング攻撃対策)で、URL を直接叩くアクセスは制御しません

つまり「誰が届けるか」はネットワーク層で設計する必要があります。それが本ガイドの主題です。

| 方法 | 向いている場面 | 難度 | CodeRouter 側の設定 |
|---|---|---|---|
| ① SSH トンネル | 個人・端末 1 台・一時的 | 低 | **不要**(loopback のまま) |
| ② Tailscale | 個人〜小規模・複数端末・外出先からも | 低 | `ALLOWED_HOSTS` に Tailscale 名/IP |
| ③ リバースプロキシ + 認証 | チーム・常設・ブラウザ利用が主 | 中 | `ALLOWED_HOSTS` に公開ホスト名 |
| ④ 素の LAN 公開 + FW | 完全に信頼できる家庭内 LAN のみ | 低 | `--host 0.0.0.0` + `ALLOWED_HOSTS` |

---

## ① SSH トンネル — 最小・最安全(個人用の第一候補)

サーバー側は**デフォルトの loopback バインドのまま**。何も公開しません。クライアント側でトンネルを掘ります。

```bash
# クライアント(例: MacBook)側で
ssh -N -L 8088:localhost:8088 you@<サーバーのIP>

# 以後、クライアントの http://localhost:8088 がサーバーの CodeRouter に繋がる
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

- `CODEROUTER_ALLOWED_HOSTS` は**不要**(Host は localhost のまま)
- 新しい攻撃面はゼロ(SSH の鍵認証がそのまま門番)
- 欠点: 端末ごと・セッションごとにトンネルを張る手間

## ② Tailscale — 複数端末・外出先(推奨)

[Tailscale](https://tailscale.com/)(WireGuard ベースのメッシュ VPN)を両方の機体に入れると、**LAN 公開そのものが不要**になります。各端末に `100.x.y.z` の私設 IP と MagicDNS 名(例: `my-server.tailnet-name.ts.net`)が付き、tailnet に参加した端末からしか届きません。

```bash
# サーバー側 — Tailscale IP でだけ待ち受け、その名前を許可
CODEROUTER_ALLOWED_HOSTS=my-server.tailnet-name.ts.net,100.x.y.z \
  coderouter-t serve --host 100.x.y.z --port 8088

# クライアント側(tailnet 参加済みなら世界中どこからでも)
open http://my-server.tailnet-name.ts.net:8088/dashboard
```

- `--host 0.0.0.0` ではなく **Tailscale IP にバインド**するのがポイント(物理 LAN 側には一切開かない)
- 認証・暗号化は Tailscale が担う(端末単位の承認、鍵ローテーション込み)
- 無料枠で個人利用は十分。外出先の Claude Code からも同じ URL で届く

## ③ リバースプロキシ + 認証 — チーム・常設公開

複数人で使う、ブラウザ中心で使う、という場合は認証付きプロキシを前段に置きます。[Caddy](https://caddyserver.com/) なら数行です。

```
# Caddyfile — basic 認証 + HTTPS(内部 CA)の例
coderouter.example.internal {
    tls internal
    basic_auth {
        alice $2a$14$...   # caddy hash-password で生成
    }
    reverse_proxy 127.0.0.1:8088
}
```

```bash
# CodeRouter は loopback のまま。プロキシが名乗る Host を許可
CODEROUTER_ALLOWED_HOSTS=coderouter.example.internal coderouter-t serve --port 8088
```

- CodeRouter 自体は外に出さない(プロキシだけが 127.0.0.1:8088 に届く)
- Claude Code から使う場合は `ANTHROPIC_AUTH_TOKEN` とは別に、プロキシの認証情報をどう渡すかを設計してください(basic 認証なら `https://user:pass@host` 形式か、ヘッダ注入できるプロキシ設定)

## ④ 素の LAN 公開 — 信頼できる家庭内 LAN 限定

家族しかいない自宅 LAN のような環境なら、シンプルにこれで動きます。

```bash
# サーバー機(例: 192.168.1.10)で — ALLOWED_HOSTS はサーバー自身のIP(URLに打つ方)
CODEROUTER_ALLOWED_HOSTS=192.168.1.10 coderouter-t serve --host 0.0.0.0 --port 8088
```

やること 2 つ:

1. **送信元をファイアウォールで絞る**(任意だが推奨): `sudo ufw allow from 192.168.1.20 to any port 8088` のように、許可する端末だけ列挙
2. **ルーターのポート開放・UPnP で 8088 が外に出ていないか確認**。グローバル IP を構内に振る環境(大学・企業の 133.x 系など)では、「LAN 内」のつもりがインターネット到達可能なことがあります

> ⚠️ 共有オフィス・学内 LAN・ゲスト Wi-Fi が同居するネットワークでは④は使わず、①〜③にしてください。LAN 内の誰でもあなたのモデルで推論できてしまいます。

---

## よくある間違い

- **`ALLOWED_HOSTS` にクライアント側の IP を入れる** — 入れるのは「クライアントの URL バーに表示される値」= **サーバー機側**のアドレスです。403 エラーメッセージに出ている値(ポート除く)をそのまま入れるのが確実([troubleshooting §1-6](./troubleshooting.md#1-6-別-pc-からアクセスすると-host--is-not-allowed-403--v270-以降))
- **`ALLOWED_HOSTS` を設定したから安全だと思う** — 冒頭のとおり、これは認証ではありません
- **`--host 0.0.0.0` のまま Tailscale を使う** — せっかくの Tailscale が台無しです。バインドは Tailscale IP に

## 関連

- [セキュリティガイド](./security.md) — 信頼境界・脅威モデルの全体像
- [トラブルシューティング §1-6](./troubleshooting.md) — `Host '...' is not allowed.` (403) の対処
