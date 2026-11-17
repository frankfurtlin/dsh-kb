# Wiki Schema

本目录由 dsh-kb 自动生成，事实源是 `reports/` 与 SQLite 索引库。

## 结构

- `pages/<slug>.md`：实体页（kind=entity），每个页 = 一个实体 + 引用它的报告。
- `index.md`：导航索引（按报告数排序）。
- `log.md`：编译日志（最近一次编译的统计）。
- `SCHEMA.md`：本文件。

## 约定

- 页内链接：`[[slug|显示名]]` 指向其它实体页；`[[report:<path>|标题]]` 指向原报告。
- 页面是**派生物**：不要只改这里，改完请重新跑 `POST /api/wiki/compile`。
- 编译是确定性的（不调模型），可以由 reports/ 随时重建。
