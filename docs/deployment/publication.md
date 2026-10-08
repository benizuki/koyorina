# 生成アプリのビルド・Push・公開

開発者はKoyorinaの「公開」で生成版をビルド・Pushし、成功した版を公開登録する。
登録だけでは起動せず、公開登録したアプリだけが「公開アプリ運用」に表示される。
運用管理者は同画面で起動・停止、公開登録済みの版への切り替えと個人・部門への利用許可を管理する。
新規ビルドのイメージにはビルドUUIDタグに加え、生成版ごとの`rev1`、`rev2`などのタグを付ける。
同じ生成版を再ビルドすると`revN`タグは最新のビルドを指す。公開アプリの起動には引き続きdigestを使用する。
公開環境変数も同画面で設定し、保存済みの値は次の起動・版の適用時に注入する。
同画面の「実行リソース」ではアプリ単位のCPU要求量・上限、メモリ要求量・上限、
公開データPVC容量を設定できる。保存した値は次の起動・版の適用時に反映する。
既存PVCは縮小できず、拡張にはStorageClassの`allowVolumeExpansion`が必要。
GCEのPD CSIでは10Gi以上を指定する。既存PVCのStorageClassは変更しない。
利用者は「利用できるアプリ」から開く。生成やビルドの成功だけでは
公開版を変えない。公開中は常時起動する。Docker Composeでは単一ホスト用の
ローカルRegistryとDocker実行環境で公開機能を試せる。容量制限や可用性などは
Kubernetes版と異なるため、詳細は[README](../../README.md#まず試すdocker-compose)を参照する。

## 配備の前提

1. 本体イメージを再ビルドし、管理アプリと公開controllerへ同じdigestを設定する。
2. 管理DBへ `python -m alembic upgrade head` を実行する（公開・リソース設定・テナントロールの追加revisionは0002に統合）。
   既存のDB移行手順を使い、移行を先に完了させる。
3. Kubernetes 1.30以上、ノードのユーザー名前空間、rootless BuildKitを実行できる
   seccomp/AppArmor設定を確認する。ビルドnamespaceは緩和するが、公開namespaceはrestricted。
   管理アプリにはビルド・公開namespaceの状態とログの読み取り権限のみ追加する。
   ビルドPodにDocker socketやhostPathは渡さない。
4. SQLite用はオンプレでCeph RBD、GCEでPD CSIの単一Pod用ブロックPVCを使う。
   新規PVCは`ReadWriteOncePod`、既存local-path PVCは削除まで現行のまま維持する。
   イメージの更新は単一レプリカのRecreate方式で、一時停止を伴う。
5. Registryの保存容量、テナント別キャッシュ容量、公開データのバックアップを確保する。
   公開データはアプリごとのPVC、ビルドキャッシュはテナントごとのPVCに保持する。

まず `setup/publication/values.private.example.yaml` または `values.artifact.example.yaml` を
ローカルのvaluesファイルへコピーし、実際の値に置き換える。インフラ用のBuildKit・Python
イメージもdigestで指定する。BuildKitはrootlessの版、Python helperは3.14の版を使う。

## 内部Registry

`appRegistryKind: private` と、Pod・全対象ノードの両方から解決できる
`appRegistryHost: registry.internal.example:30500` を指定する。
クラスタService名（`.svc`）はノードのcontainerdから名前解決できる前提にしない。

`publication.internalRegistry.enabled: true` でBasic認証付きのRegistryを作れる。
Ansibleでは既定でHTTPを使い、証明書は不要。`publication.registryHttp: true` が対応するHelm設定。
TLSを使う場合はAnsibleの `forge_publication_registry_tls_enabled: true`（HelmではregistryHttp=false）にする。
以下のTLS用Secret・CAの設定はTLS使用時のみ必要。
事前に `<release>-registry` namespaceへ `registry-tls`（tls.crt/tls.key）と
`registry-auth`（bcrypt形式のhtpasswd）を作る。証明書のSANはRegistryのホスト名に合わせる。
NodePort 30500の接続元はクラスタのPod・ノードに限定するよう、ノード側ファイアウォールで設定する。
既存の開発用Registryがある場合は、そのPVCをバックアップし、既存DeploymentをTLS・認証付きへ
移行する。既存リソースをHelmへ取り込む際は所有情報を確認し、PVCを削除して作り直さない。

`<release>-build` には `registry-push`（kubernetes.io/dockerconfigjson）と
`registry-ca`（ca.crt）を作る。Push資格情報は管理アプリ・公開Podへ渡さない。
Basic認証だけのDistribution Registryは利用者別の細かなリポジトリ権限を持たないため、
ノードに厳密なPull専用権限が必要なら認証サービス付きのRegistryを使用する。

各ノードの `/etc/rancher/k3s/registries.yaml` にHTTPまたはHTTPS endpointと認証を設定する。
HTTPSの場合のみCAを設定する。HTTPでは通信が暗号化されないため、接続元を基盤に限定する。
Ansibleでは `forge_publication_enabled: true` と `forge_publication_registry_*` を指定すると
`publication-node` ロールが既存設定を保持して追加する。パスワードはAnsible Vaultに置く。
設定変更時にk3s/k3s-agentを再起動するため、メンテナンス時間に実行する。

## システム設定で保存先を切り替える

管理者の「システム設定」→「生成アプリのイメージ保存先」で、内部RegistryとArtifact Registryを選ぶ。
保存した設定は次のビルドから適用する。各ビルドに保存先・WIF設定を固定して記録するため、
設定変更・コントローラー再起動後も旧版の公開・切り戻しに以前の保存先を使う。
既存イメージのコピーや削除は行わない。過去版を使う間は以前のARリポジトリとWIF/IAMを維持する。
内部Registryの接続先・HTTP/TLS・認証・PVCはAnsibleで配備する。画面から接続先を変更しない。

## Artifact Registry（オンプレ・GCE共通）

実行場所にかかわらずARを選べる。リポジトリの指定は `REGION-docker.pkg.dev/PROJECT/REPOSITORY`。
画面でWIFプールのプロジェクト番号・プールID・OIDCプロバイダーIDを指定する。
Push用・Pull用のGoogle Cloudサービスアカウントは個別に任意指定できる。
空欄なら各Kubernetes ServiceAccountのSubjectへリポジトリ権限を直接付与する。
指定した側だけサービスアカウントをなりすまし、鍵ファイルは使用しない。

「WIFの設定手順」からクラスタのJWKSを取得し、画面のコマンドでGCPのAPI・リポジトリ・
プール・プロバイダー・IAMを設定する。既存リソースはcreateせず確認・更新する。
なりすまし先を指定する場合、そのサービスアカウントはGCPに事前作成する。
プロバイダーはクラスタ発行の公開鍵を使い、以下の2つのsubjectだけを信頼する。

- Push: `system:serviceaccount:<release>-build:publication-controller`
- Pull: `system:serviceaccount:<release>-build:publication-puller`

直接アクセスでは、生成アプリ用リポジトリだけにPushのSubjectへ `roles/artifactregistry.repoAdmin`（古いビルドのイメージの削除を含む）、
PullのSubjectへ `roles/artifactregistry.reader` を付与する。なりすまし方式では、指定したGCPサービスアカウントに
対応するSubjectからの `roles/iam.workloadIdentityUser` を付与し、リポジトリ権限はそのサービスアカウントへ付与する。
オンプレのOIDC URLをインターネット公開する必要はない。署名鍵の更新時はGCPのJWKSも更新する。

コントローラーは名前を限定したTokenRequestでOIDCトークンを取得し、WIFで短期access tokenへ交換する。
ビルドPodにはPush用の短期Secretだけを渡し、ビルド終了時に削除する。
公開PodのimagePullSecretsにはPull用トークンを設定し、稼働中・更新中・復旧候補について30分ごとに更新する。
ノードにGCP鍵や固定access tokenを配る必要はない。ノードからARへHTTPSで到達できることは必要。

「接続確認」は両方のWIF認証・リポジトリアクセス・Push/PullのIAM権限を確認する。
実際のBuild→Push→ノードPull→公開は別途スモークテストで確認する。
脆弱性検査はGCPで有効化した場合だけ画面で「有効化済み」を選ぶ。

## イメージの保持と削除

ビルドのたびにイメージが増えるため、公開コントローラーが保持ルールに沿って古いものを消す。
ビルドが成功したときと、プロジェクトを削除したときに判定する。

- 残す: 公開中のビルド、実行中のビルド、成功したビルドの新しい`PUBLICATION_KEEP_BUILDS`件（既定5）、
  失敗・中止したビルドの新しい同件数
- 消す: それ以外のイメージとビルド履歴（誰がいつビルドしたかは監査ログに残る）
- 同じ中身のビルドは同じダイジェストになる。残すビルドと同じダイジェストのイメージは消さない
- 同じプロジェクトでビルドが動いている間は消さない（push中のイメージを消さないため）

内部Registryは`REGISTRY_STORAGE_DELETE_ENABLED=true`で削除を受け付ける。削除はマニフェストを
外すだけなので、毎日`publication.internalRegistry.gcHourUtc`（既定18 = 日本時間3時）に
公開コントローラーが`registry garbage-collect --delete-untagged`のJobを走らせて領域を空ける。
Registryは止めない（pullは続く）。実行中はビルドの受付と削除を止め、ビルドが無い時を選ぶ。

Artifact Registryは同じAPIで消す。Push用の身元に`artifactregistry.versions.delete`が要る
（`roles/artifactregistry.repoAdmin`）。無い場合はビルドと公開は動くが、古いイメージが溜まり続ける。
「接続確認」で不足を知らせる。既存のリポジトリには次で付与する。

```sh
gcloud artifacts repositories add-iam-policy-binding <リポジトリ> --project=<プロジェクトID> --location=<リージョン> \
  --role=roles/artifactregistry.repoAdmin --member=<Push用のmember>
```

タグの無いもの（上書きされたビルドキャッシュ）は、GCEではTerraformのクリーンアップポリシーで
7日後に消す。件数や古さだけで消すポリシーは使わない（公開中のイメージまで消えるため）。

## 有効化

既存のk3s配備（生マニフェスト・Helmのどちらでも）に追加する場合:

```sh
python3 setup/publication/configure.py --context YOUR_CONTEXT --release koyorina \
  --values /path/to/publication-values.yaml --render-only
python3 setup/publication/configure.py --context YOUR_CONTEXT --release koyorina \
  --values /path/to/publication-values.yaml
```

このスクリプトは公開リソースを適用し、既存の管理Deploymentへ公開用環境変数を追加する。
サービス間認証鍵は既存値を保持してペアを作り、不一致なら上書きせず停止する。
Operator側にhelm、kubectl、PythonとPyYAMLが必要。管理コンテナへhelmは不要。
Helmで運用する場合は、同じvaluesを今後のupgradeにも必ず渡す。
生マニフェスト運用でapplyし直す場合は、configure.pyも再実行する。

アプリ側の設定は `PUBLICATION_ENABLED`、`PUBLICATION_CONTROLLER_URL`、
`PUBLICATION_CONTROLLER_TOKEN` と既存の `APP_REGISTRY_KIND` / `APP_REGISTRY_HOST`。
同時ビルド数・実行時間は `publication.maxBuilds` / `publication.buildTimeout` を変更し、
controllerを再配備する。既定は1件・20分。同一テナントのキャッシュは1ビルドだけが書く。

## 配備検証

既存のテナントロールは移行時に従来の権限へ展開する。管理者は管理者・開発者・運用管理者・利用者、
開発者は開発者・運用管理者・利用者、利用者は利用者となる。移行後はロールを独立に編集できる。
運用管理者ロールを持つ利用者は担当テナントの公開アプリを一元管理できる。テナント管理者は
既存メンバーのロールを変更でき、所属の追加・削除はシステム管理者が行う。
利用者ロールに加え、公開アプリごとの個人・部門の許可が必要。権限変更は次のアクセスから反映する。

```sh
python3 setup/publication/preflight.py --context YOUR_CONTEXT --release koyorina \
  --registry-kind private --registry-host registry.internal.example:30500 \
  --node user@node-1 --node user@node-2 \
  --pull-image registry.internal.example:30500/known-image@sha256:ACTUAL_DIGEST
```

preflightはStorageClass・ユーザー名前空間・全対象ノードのcontainerd Pullを確認する。
`--pull-image` はノードのイメージキャッシュを書き込むが、アプリやDBは変更しない。
対象ノードまたはPull試験を省略した場合は、検証完了として扱わない。
Rootless BuildKitが実際に動くことは、続いて専用テストアプリでビルド・Pushして確認する。

両Registryについて、以下を実環境で確認する:

- ビルド→Push→公開登録→運用画面でdigest固定の起動→許可された利用者のアクセス。
- 個人許可、部門許可、別テナント、利用許可解除、部門変更・無効ユーザー。
- ビルド失敗・Push失敗・中止、controller再起動、起動失敗時の直前イメージへの復旧。
- 公開環境へのレコード保存後、再起動・更新・公開停止・再公開でデータが保持されること。
- ビルド中のソース編集が成果物へ混ざらないこと、プレビューのDBや秘密が混ざらないこと。

## 初期版の運用境界

公開データは初回公開時に空で始まり、プレビューから移行しない。公開環境変数も別に保存する。
秘密は管理DBで暗号化し、Kubernetes Secret経由で実行時に注入する。
保存した環境変数は次の公開操作で反映する。

DBスキーマ変更の自動承認・移行は行わない。互換性を確認した版を公開する。
イメージの切り戻しでデータやスキーマは戻らない。DBのバックアップ・移行は運用者が行う。
公開前後のデータ互換性を確認してから更新する。

公開停止はDeploymentを停止し、PVC・イメージ・履歴を保持する。
公開アプリを停止し、実行中のビルドがなければテナント移行できる。公開データのPVCと
イメージ・ビルド履歴は保持し、ビルドと公開の記録を移動先テナントへ紐付け直す。
利用者・部門の許可は移動時に解除するため、移動先の運用管理者が再設定する。
保持対象があるプロジェクトの削除は、孤立した公開Podやデータを作らないため拒否する。
開発履歴のリセットは公開成果物に影響しない。
イメージ・キャッシュの自動削除やGCは初期版では行わないため、容量を定期確認する。

WIFの方式は [Google Cloudの自己管理Kubernetes向け手順](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-kubernetes) に従う。
アップロードしたJWKSを使うため、オンプレクラスタのOIDC discoveryを外部公開しない。

## Podの状態と起動失敗の確認

管理者の「接続・公開環境」で、公開基盤が有効な場合は `<release>-build` と
`<release>-published` のPod・Service・DeploymentとPodログを確認できる。
状態には `ImagePullBackOff` や `CrashLoopBackOff`、配置できない理由を表示する。
公開時の起動確認が失敗した場合は、Pod削除前の失敗理由を公開状態へ保存する。

内部Registryの「HTTPで接続する」は、接続テストと公開時に全ノードのPull設定へ反映する。
`registry-node` DaemonSetがAnsibleで指定したホスト:ポート専用の`hosts.toml`を同期し、
containerdが次のPullから使用する。k3sの再起動は不要。k3sが生成するディレクトリとは別の、
containerdが優先する`ホスト_ポート_`ディレクトリを使用し、ノード再起動後も設定を保持する。
CAなど既存設定を保持し、HTTPS指定時は証明書検証を省略しない。
同期Podには専用ディレクトリの書き込みとcontainerd設定・元のRegistry設定の読み取りだけを与え、
Docker/CRIソケット、ホストのルート、ホストネットワークは渡さない。

初回は新しい本体イメージを配備してからAnsibleの`site.yml`を再適用する。
公開処理は全ノードの反映報告を確認してからDeploymentを作成し、未配備・反映失敗時は
具体的な案内を返す。接続先変更はAnsibleも更新する。再適用では画面で選んだ接続方式を保持する。
ユーザー名・パスワードは実行Podの`imagePullSecrets`で渡し、同期Podには渡さない。

HTTP/HTTPSのエラーが残る場合は、`registry-node`の状態と公開Podの失敗理由を確認する。
kubeletのHTTPSエラーはHTTPでの失敗後の再試行結果である場合があるため、
ノードの`/var/lib/rancher/k3s/agent/containerd/containerd.log`でも最初の失敗を確認する。

## 公開タブのビルド詳細

公開タブの「ビルドログ」「Dockerfile」を切り替えて、直近のビルドの詳細を確認できる。
ビルドを開始するとそのログを自動表示し、実行中は5秒ごとに更新する。
ログには末尾追従・コピー・手動更新を設ける。利用許可は「公開アプリ運用」で「人」「部門」の2列に表示する。
新規ビルドでは使用する管理Dockerfileをビルド要求時に固定し、ビルドPod削除後も保持する。
BuildKitはビルドごとにベースイメージの最新版を解決し、最終ステージのAPT更新を実行する。
修正済みのDebianパッケージを取り込み、`ca-certificates`と`libsqlite3-0`を実行環境に入れる。
この更新はビルド時間を増やす。公開中のイメージは自動更新されないため、新しい版をビルド・デプロイする。
保存のない過去ビルドでは、現在のDockerfileを使用済みの内容として表示しない。
