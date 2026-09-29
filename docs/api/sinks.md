---
description: fujilib.sinks — wide rows, fixed schemas, the memory, CSV and Parquet sinks, and pipe().
---

# `fujilib.sinks`

`sample_to_row()` flattens a sample into one wide row whose columns
`row_columns()` fixes from the channels. A sink locks those columns before its
first row and writes samples; `pipe()` writes a recording to one (design §7.6).

::: fujilib.sinks.base

::: fujilib.sinks._schema

::: fujilib.sinks.memory

::: fujilib.sinks.csv

::: fujilib.sinks.parquet
