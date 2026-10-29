# P7 十条检索样本

来自已验收 P2 二十万商品；仅结果观察，不是召回率或性能基线。商品名是“合成商品·葡语 Olist 类目·序号”，并非真实商品描述；attrs 为空。

全文文档使用 PostgreSQL english 配置，对标题、SKU、属性文本生成词元；下表保留数据库实际 tsquery。它不具备中文分词或语义翻译。输入中文“电水壶”零结果是当前限制。

排序为 ts_rank_cd 降序、商品价+运费升序、商品ID升序；先过滤 active、价格、类目、属性和可用库存。导购在最多100个候选中选择预算内最低总价，不能称全库最优。金额以分为单位，样本不证明语义理解或排序质量；性能基线留 P10/S3。

## 1. 浏览有库存商品

查询 ``；过滤 `{"query": "", "category_id": null, "min_price_cent": 0, "max_price_cent": 9223372036854775807, "attributes": {}, "available_only": true, "limit": 5, "offset": 0}`；实际 tsquery ``。

|顺位|ID / 商品名|价格 / 运费 / 总价（分）|可用库存|rank|命中依据|
|---|---|---|---|---|---|
|1|701 / 合成商品·beleza_saude·701|120 / 789 / 909|1000|0.000000|active、库存>0、结构过滤通过；空关键词浏览，无全文匹配要求|
|2|5159 / 合成商品·beleza_saude·5159|120 / 789 / 909|1000|0.000000|active、库存>0、结构过滤通过；空关键词浏览，无全文匹配要求|
|3|40862 / 合成商品·beleza_saude·40862|120 / 789 / 909|1000|0.000000|active、库存>0、结构过滤通过；空关键词浏览，无全文匹配要求|
|4|41190 / 合成商品·beleza_saude·41190|120 / 789 / 909|1000|0.000000|active、库存>0、结构过滤通过；空关键词浏览，无全文匹配要求|
|5|50208 / 合成商品·beleza_saude·50208|120 / 789 / 909|1000|0.000000|active、库存>0、结构过滤通过；空关键词浏览，无全文匹配要求|

## 2. 香水类别

查询 `perfumaria`；过滤 `{"query": "perfumaria", "category_id": null, "min_price_cent": 0, "max_price_cent": 9223372036854775807, "attributes": {}, "available_only": true, "limit": 5, "offset": 0}`；实际 tsquery `'perfumaria'`。

|顺位|ID / 商品名|价格 / 运费 / 总价（分）|可用库存|rank|命中依据|
|---|---|---|---|---|---|
|1|20865 / 合成商品·perfumaria·20865|700 / 256 / 956|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|2|36122 / 合成商品·perfumaria·36122|700 / 256 / 956|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|3|102943 / 合成商品·perfumaria·102943|700 / 256 / 956|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|4|112606 / 合成商品·perfumaria·112606|700 / 256 / 956|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|5|188734 / 合成商品·perfumaria·188734|700 / 256 / 956|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|

## 3. 汽车用品

查询 `automotivo`；过滤 `{"query": "automotivo", "category_id": null, "min_price_cent": 0, "max_price_cent": 9223372036854775807, "attributes": {}, "available_only": true, "limit": 5, "offset": 0}`；实际 tsquery `'automotivo'`。

|顺位|ID / 商品名|价格 / 运费 / 总价（分）|可用库存|rank|命中依据|
|---|---|---|---|---|---|
|1|20049 / 合成商品·automotivo·20049|690 / 368 / 1058|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|2|43963 / 合成商品·automotivo·43963|690 / 368 / 1058|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|3|86663 / 合成商品·automotivo·86663|690 / 368 / 1058|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|4|106184 / 合成商品·automotivo·106184|690 / 368 / 1058|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|5|116483 / 合成商品·automotivo·116483|690 / 368 / 1058|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|

## 4. 音响商品

查询 `audio`；过滤 `{"query": "audio", "category_id": null, "min_price_cent": 0, "max_price_cent": 9223372036854775807, "attributes": {}, "available_only": true, "limit": 5, "offset": 0}`；实际 tsquery `'audio'`。

|顺位|ID / 商品名|价格 / 运费 / 总价（分）|可用库存|rank|命中依据|
|---|---|---|---|---|---|
|1|28932 / 合成商品·audio·28932|1990 / 778 / 2768|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|2|50443 / 合成商品·audio·50443|1990 / 778 / 2768|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|3|140011 / 合成商品·audio·140011|1990 / 778 / 2768|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|4|148861 / 合成商品·audio·148861|1990 / 778 / 2768|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|5|31063 / 合成商品·audio·31063|1490 / 1291 / 2781|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|

## 5. 玩具

查询 `brinquedos`；过滤 `{"query": "brinquedos", "category_id": null, "min_price_cent": 0, "max_price_cent": 9223372036854775807, "attributes": {}, "available_only": true, "limit": 5, "offset": 0}`；实际 tsquery `'brinquedo'`。

|顺位|ID / 商品名|价格 / 运费 / 总价（分）|可用库存|rank|命中依据|
|---|---|---|---|---|---|
|1|6769 / 合成商品·brinquedos·6769|690 / 739 / 1429|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|2|6826 / 合成商品·brinquedos·6826|690 / 739 / 1429|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|3|80370 / 合成商品·brinquedos·80370|690 / 739 / 1429|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|4|83987 / 合成商品·brinquedos·83987|690 / 739 / 1429|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|5|100684 / 合成商品·brinquedos·100684|690 / 739 / 1429|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|

## 6. 美容健康

查询 `beleza_saude`；过滤 `{"query": "beleza_saude", "category_id": null, "min_price_cent": 0, "max_price_cent": 9223372036854775807, "attributes": {}, "available_only": true, "limit": 5, "offset": 0}`；实际 tsquery `'beleza' & 'saud'`。

|顺位|ID / 商品名|价格 / 运费 / 总价（分）|可用库存|rank|命中依据|
|---|---|---|---|---|---|
|1|701 / 合成商品·beleza_saude·701|120 / 789 / 909|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|2|5159 / 合成商品·beleza_saude·5159|120 / 789 / 909|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|3|40862 / 合成商品·beleza_saude·40862|120 / 789 / 909|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|4|41190 / 合成商品·beleza_saude·41190|120 / 789 / 909|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|5|50208 / 合成商品·beleza_saude·50208|120 / 789 / 909|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|

## 7. 汽车用品不超过100元

查询 `automotivo`；过滤 `{"query": "automotivo", "category_id": null, "min_price_cent": 0, "max_price_cent": 10000, "attributes": {}, "available_only": true, "limit": 5, "offset": 0}`；实际 tsquery `'automotivo'`。

|顺位|ID / 商品名|价格 / 运费 / 总价（分）|可用库存|rank|命中依据|
|---|---|---|---|---|---|
|1|20049 / 合成商品·automotivo·20049|690 / 368 / 1058|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|2|43963 / 合成商品·automotivo·43963|690 / 368 / 1058|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|3|86663 / 合成商品·automotivo·86663|690 / 368 / 1058|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|4|106184 / 合成商品·automotivo·106184|690 / 368 / 1058|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|5|116483 / 合成商品·automotivo·116483|690 / 368 / 1058|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|

## 8. 玩具10到50元

查询 `brinquedos`；过滤 `{"query": "brinquedos", "category_id": null, "min_price_cent": 1000, "max_price_cent": 5000, "attributes": {}, "available_only": true, "limit": 5, "offset": 0}`；实际 tsquery `'brinquedo'`。

|顺位|ID / 商品名|价格 / 运费 / 总价（分）|可用库存|rank|命中依据|
|---|---|---|---|---|---|
|1|9355 / 合成商品·brinquedos·9355|1049 / 758 / 1807|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|2|61954 / 合成商品·brinquedos·61954|1049 / 758 / 1807|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|3|91092 / 合成商品·brinquedos·91092|1049 / 758 / 1807|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|4|128270 / 合成商品·brinquedos·128270|1049 / 758 / 1807|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|5|129691 / 合成商品·brinquedos·129691|1049 / 758 / 1807|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|

## 9. 音响限定类目8

查询 `audio`；过滤 `{"query": "audio", "category_id": 8, "min_price_cent": 0, "max_price_cent": 9223372036854775807, "attributes": {}, "available_only": true, "limit": 5, "offset": 0}`；实际 tsquery `'audio'`。

|顺位|ID / 商品名|价格 / 运费 / 总价（分）|可用库存|rank|命中依据|
|---|---|---|---|---|---|
|1|28932 / 合成商品·audio·28932|1990 / 778 / 2768|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|2|50443 / 合成商品·audio·50443|1990 / 778 / 2768|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|3|140011 / 合成商品·audio·140011|1990 / 778 / 2768|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|4|148861 / 合成商品·audio·148861|1990 / 778 / 2768|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|
|5|31063 / 合成商品·audio·31063|1490 / 1291 / 2781|1000|0.100000|active、库存>0、结构过滤通过；标题类目词元满足 tsquery|

## 10. 中文电水壶

查询 `电水壶`；过滤 `{"query": "电水壶", "category_id": null, "min_price_cent": 0, "max_price_cent": 9223372036854775807, "attributes": {}, "available_only": true, "limit": 5, "offset": 0}`；实际 tsquery `'电水壶'`。

|顺位|ID / 商品名|价格 / 运费 / 总价（分）|可用库存|rank|命中依据|
|---|---|---|---|---|---|

无命中，前5条为空；没有替换成演示或伪造结果。
