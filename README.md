# Sky Survey ZIP Fetch（巡天取包服务）

巡天分析员选定观测号后，先通过 HTTP Range 读取 ZIP 中央目录，再按需拉取单个
FITS 成员。服务把多个**本地源包**（每包一个 Deflate 压缩的 FITS）在内存中
虚拟组装成一个标准 ZIP 流式输出：**压缩数据原样复用**，磁盘上只额外保存
清单与索引（SQLite）。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs 试用（Swagger UI）
```

源包放入 `./data/sources/`（容器内 `/data/sources`，服务只读访问），SQLite
清单/索引在 `./data/zipfetch.db`。两者都在 bind mount 上，重启、换版后旧
取包链接仍然有效。

不用 Docker 本地跑：

```bash
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --port 8000
```

## 工作流程

1. **登记新版**（二选一）：
   - 运营方把源包放进 `./data/sources/`，然后
     `POST /observations/{obs_id}/versions`  `{"path": "obsA/v2.zip"}`
     （相对 sources 的路径）；
   - 或直接 `POST /observations/{obs_id}/upload`（multipart 上传，服务存到
     `sources/<obs_id>/v<N>.zip`）。
2. **下单**：`POST /orders`  `{"obs_ids": ["OBS-A", ...]}`（1–32 个，不重复）。
   响应里的 `url` 即取包链接，`members[]` 给出每个成员在组包中的精确偏移。
3. **取包**：`GET /orders/{order_id}/zip`
   - 整包：`GET`（或 `HEAD` 取 `Content-Length`）；
   - 单段 Range：`Range: bytes=<start>-<end>` / `bytes=<start>-` / `bytes=-<suffix>`，
     返回 206 与 `Content-Range`；跨成员、跨中央目录的区间同样精确，
     且只从源包读取相交的压缩数据窗口；
   - 多段 Range 不支持（回退 200 整包）；不可满足返回 416。

观测号/版本查询：`GET /observations`、`GET /observations/{obs_id}`、
`GET /orders`、`GET /orders/{order_id}`。

## 实现要点

- **源包校验（登记时）**：定位 EOCD，要求恰好 1 个成员、method=8(Deflate)、
  无加密(flag bit0)、无 ZIP64（各项尺寸/偏移均非 0xFFFFFFFF 哨兵）、非分卷；
  然后**完整解压一次**核对 CRC-32、解压长度，并检查 FITS `SIMPLE` 魔数。
  任何一步失败 → 422，**不触碰现行版本**（校验先于写库，写库单事务，
  失败回滚保留原版）。源包路径全库唯一（`UNIQUE(source_path)`），新包必须
  使用新路径，从流程上杜绝"原地换包"导致旧单字节被偷换。
- **固定（pin）**：下单在 `BEGIN IMMEDIATE` 单事务里读取全部观测号的现行
  版本（同一一致性快照），并核对源文件 size+mtime_ns 未变；任一观测号未知
  或源包不可用 → 409 **退回整单**，不落任何数据。组包总尺寸 ≥ 4 GiB → 422。
- **虚拟组包**：成员按 `<obs_id>.fits` 命名；本地头/中央目录/EOCD 现算
  （method、CRC、尺寸、DOS 时间戳照抄源包中央目录），压缩数据以
  `(源文件, 偏移, 长度)` 窗口引用，流式拼接。清单（`orders`/`order_members`）
  与索引（`observations`/`versions`）存 SQLite(WAL)，**组包永不落盘**。
- **Range 流式输出**：把虚拟文件视为"生成字节段 + 文件窗口段"序列，按请求
  区间切片；文件段 seek 后只读相交部分（256 KiB 块），跨成员/跨目录自然
  精确。`Content-Length` 恒定可知，支持 HEAD。
- **旧链接保真**：订单行固化版本号、源路径、数据偏移、文件 size/mtime。
  换版（新文件新路径）或重启后，旧链接返回原字节；若源文件被意外改动/
  删除（size 或 mtime 不符），下载返回 410、下单返回 409，而不是悄悄给错
  字节。

## 验证结果（自测）

`tests/selftest.py` 两阶段共 **96 项断言全部通过**（FastAPI 0.165 /
uvicorn 0.54 / Python 3.11）：

- 登记：4 个观测号 v1 成功；两成员、STORED、加密标志、非 FITS、截断包、
  缺失文件、路径穿越、重复路径登记均被 422 拒绝且**原版保留**。
- 下单校验：空单/重复/33 个超限 → 422；未知观测号 → 409 整单退回。
- 整包：Python `zipfile` 解压逐字节等于原始 FITS；`testzip` CRC 通过；
  外部工具 `unzip -t`、`zipinfo -v` 验证为标准 ZIP（3 成员、deflate、
  Unix 0644 属性）；EOCD 计数/偏移与清单一致。
- **原样复用**：组包中每个成员的压缩字节与源包压缩窗口逐字节相等，CRC
  与源包中央目录一致。
- Range：首 100 字节、跨成员边界、深入 1.5 MB 大成员、末成员→中央目录、
  中央目录→EOCD、整包区间、后缀区间、单字节 → 206 且字节精确；
  越界 → 416（`Content-Range: bytes */N`）；多段/倒置区间 → 200 整包；
  HEAD 返回正确长度无正文。
- **换版后续传**：OBS-A 登记 v2、OBS-B 上传 v2 后，旧单链接字节不变
  （SHA-256 一致、内容仍是 v1），新单含新版；**重启进程后**三个旧单链接
  字节不变，服务可正常接新单。
- 源包可用性：源文件被移走 → 下单 409、下载 410；mtime 被篡改 → 409；
  恢复后正常。32 成员上限单 + 4 GiB 布局边界（差 1 字节通过、恰好 4 GiB
  拒绝）均验证。

复现（本机，无需 Docker）：

```bash
pip install -r requirements.txt httpx
bash tests/run_local.sh        # 起服务→pre-restart→重启→post-restart
```

（`tests/serve_ttl.py` 仅供信号受限的沙箱环境自毁重启用，非交付路径。）

## 未完成项 / 已知限制

- **多段 Range**（`multipart/byteranges`）未实现，按 RFC 回退 200 整包。
- 源包不可变性靠"新包新路径 + size/mtime 校验"约束；运营方若用硬链接/
  同步工具绕过并原地改字节，无法 100% 检测（校验的是元数据非内容哈希，
  这是"磁盘只存清单索引"约束下的取舍）。
- 登记时的全量解压校验是 O(包大小) 的一次性开销；超大包登记会变慢。
- 上传接口未做鉴权/配额；生产部署应放在认证网关之后，容器可改非 root
  运行（需相应 chown `./data`）。
- 无自动化清理：历史版本源包与 `.incoming` 残留需运营方按需清理
  （旧订单依赖旧源包，删除前请确认无在效订单引用）。
