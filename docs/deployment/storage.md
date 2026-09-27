# 作業領域のストレージ: local-path か Filestore/NFS か

## 既定: local-path（ReadWriteOnce、ノード固定）

生成コード・ジョブ・履歴（テナントごとのPVC）とプレビューの作業領域は、
既定では k3s 内蔵の `local-path` provisioner を使う。  


## すでにCeph等RWX対応のStorageClassがある場合

CephFSのStorageClass（RWX対応）がすでにあるなら、Filestore/NFSは要らない。
`setup/ansible` の `forge_storage_mode: external` を選ぶと、Ansibleは
csi-driver-nfsのインストールもStorageClassの作成も行わず、指定した
StorageClass名（例: `cephfs`）が実在することだけ確認する。

```yaml
# setup/ansible/group_vars/onprem/defaults.yml 等
forge_storage_mode: external
forge_storage_class: cephfs
forge_storage_access_mode: ReadWriteMany
forge_node_selector: {}   # Cephはどのノードからもマウントできるのでノード固定は不要
```

以下の「プレビューのSQLiteだけは別扱い」「テナント分離との関係」は、
ドライバがcsi-driver-nfsかCephかによらず同じように当てはまる（SQLiteの
扱い、ノード侵害時の影響範囲）。容量制御・スナップショット・バックアップ
方針は運用者側のCeph/Rook構成に委ねる。

## RWX(Filestore/NFS)を選ぶ理由と選ばない理由

Filestore/NFSに切り替えると、全PVCが **ReadWriteMany** になり、生成・
プレビューPodがどのエージェントノードにもスケジュールできるようになる。
複数エージェントで実際に負荷分散したい場合はこちらが要る。

使用する場合は、`enable_filestore` を`true`に設定し、既定は `false`。

オンプレでは任意のNFSサーバ(`nfs-kernel-server`等)のIPを指定するだけで、
同じ`setup/ansible/roles/storage` が使える。

## プレビューのSQLiteだけは別扱い

生成アプリのプレビュー用SQLite（`var/db/preview.db`）は、RWXにしてもノード
ローカルの`emptyDir`に置く。SQLiteのfcntlロックがNFS越しでは信頼できず、
`database is locked`や最悪データ破損につながるため。

## テナント分離との関係

RWXに切り替えても、開発中のソースコードを保持するPVCがテナントごとに
1つ、という分離モデル自体は変わらない。ただし、どれか1ノードのrootを
取られた攻撃者が、NFSマウント経由で全テナントのデータに届くようになる
点はローカルストレージ運用と異なる（ノード侵害時に及ぶ範囲が広がる）。

ストレージ方式は、初回デプロイ前に決める。運用開始後に変更する場合は、
既存データを退避・移行してからPVCを切り替える必要がある。
