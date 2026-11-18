# allo-font-audit

对**已发布**的字体目录做一次可复现的产物审计：把 README/合约里写的家族名、字重、轴范围、
OpenType 特性、字符覆盖、默认实例度量，逐条对到二进制文件上。为什么现在需要它：字体构建脚本
通常只断言“构建过程”，发布目录里真正发出去的字节、清单和声明之间没有任何独立核对。

Upstream: https://github.com/joshpuckett/allo

## 上游是什么、它缺什么

[Allo](https://github.com/joshpuckett/allo) 是一个 OFL 授权的可变字体家族：1 个三轴可变字体
（`opsz` 9–32 / `wght` 100–900 / `ROND` 0–100）加 18 个静态 OTF。README 在
[L83](https://github.com/joshpuckett/allo/blob/main/README.md#L83) 声称“静态字体就是可变字体在
opsz 14, ROND 50”，但在 [L124–L125](https://github.com/joshpuckett/allo/blob/main/README.md#L124-L125)
又说静态源切出后被“单独编辑”过。这两句互相矛盾，而仓库里没有工具能对**发布产物**判定哪句成立：
`build.py` 的断言只覆盖构建时的 hint zone（[L162](https://github.com/joshpuckett/allo/blob/main/build.py#L162)），
不检查 `fonts/SHA256SUMS`、18 个静态文件、家族名、轴范围或默认实例。

## 这个工具做什么、不做什么

做四类可断言的检查，全部读 SFNT 二进制的公开表（`name` / `OS/2` / `head` / `hhea` / `hmtx` /
`cmap` / `fvar` / `GSUB` / `GPOS`），不联网、不渲染、零第三方依赖：

1. `SHA256SUMS` ↔ 实际文件 ↔ 发布目录三向核对（缺失、哈希不符、清单外字体）。
2. 合约 ↔ 文件：家族名、静态字重、斜体位、`fvar` 轴 min/default/max、声明过的 OpenType 特性。
3. 家族内部一致性：18 个静态字体的 cmap 覆盖、垂直度量、特性覆盖是否一致。
4. 默认实例核对：Regular 静态字体与可变字体默认坐标（14/400/50）的 cmap→advance 是否逐字符相等。

不做：不渲染字形、不比较 CFF 与 glyf 轮廓、不对非默认坐标做 `gvar` 插值、不解析 WOFF2 内部表
（只核对它的哈希与大小）。

## 本轮创新

B 档：第一次把字体 README 的“静态字体 = 可变字体某实例”从文案变成**可执行的发布产物不变量**，
并给出真实发布目录的判定结果——不是再写一个泛化的字体 QA 规则集。对 Allo 1.0.1 的实测结论是：
39 个发布文件哈希全对、18 个静态文件全在、3 条轴范围全对、18 个声明特性全在、697 个字符的 cmap
与 advance 在 Regular 静态与可变默认实例之间**逐项相等**（`input_sha256=7b04fdd0…7068c`）。
所以 L83 的“同设计”在**度量与字符覆盖**上成立；轮廓级是否相等本工具不能证明，见局限。

## 与同类项目的差异

检索过 `fonttools/fontspector`（122★，fontbakery 后继）与 `fonttools/setup-fontspector`。
它们做通用字体质量规则（上百条 shaping/metadata 检查），需要 fontTools 生态；本工具只做一件事：
**发布目录 ↔ 项目自己的合约 ↔ SHA256SUMS** 的闭环，纯 stdlib、单文件、离线、退出码可进 CI。
两者可以并用：fontspector 管字形质量，本工具管“发出去的这批文件是否就是声明的那批”。

## 安装 / 用法

需要 Python 3.9+，无安装步骤。`examples/in/` 里的两个 Allo 字体按上游 OFL-1.1 分发，见 `OFL.txt`；代码本身是 MIT。

```sh
python3 allo_font_audit.py examples/in --contract examples/in/contract.json
```

带 JSON（可直接被 `python3 -c "import json,sys;json.load(sys.stdin)"` 解析）：

```sh
python3 allo_font_audit.py examples/in --contract examples/in/contract.json --json
```

真实发布目录（先按 README 下载 `fonts/` 与 `fonts/SHA256SUMS`）：

```sh
python3 allo_font_audit.py /path/to/allo --contract contract.json --json --output /tmp/report.json
```

退出码：`0` 无 finding；`1` 有 finding（每个 finding 带 `code`/`severity`/`files`/`detail`）；
`2` 用法或输入错误（合约缺失/JSON 坏、root 不存在、SHA256SUMS 坏）。

## 实测（全部来自真实运行）

- 真实上游 Allo 1.0.1 发布目录：39 个字体/网页字体文件、19 个 SFNT 解析、18 个静态字体，
  **0 error / 0 warning**，`input_sha256=7b04fdd04862bcc689ca4d40e53b5fb1b43ab747eae80a49d32aa476a787068c`。
- 测试：`python3 -m unittest discover -s tests` → **17/17 OK**，其中 6 个是驱动 CLI 的行为用例
  （退出码 0/1/2、JSON、离线一致性）。
- 离线：`http_proxy=https_proxy=http://127.0.0.1:9` 下 `input_sha256` 与在线一致。

## 局限

- 只比较**默认坐标**的 advance，不做 `gvar` 插值；其它 8 个字重的静态字体无法用它证明“等于可变实例”。
- CFF（静态 OTF）与 glyf（可变 TTF）轮廓格式不同，本工具不比较轮廓，只比较字符映射与度量。
- WOFF2 是 Brotli 压缩容器，只核对哈希/大小，不解析内部 `fvar`/`name`。
- 合约是人工声明的；合约本身写错时工具会忠实地报“不一致”，不会去读 README 猜意图。