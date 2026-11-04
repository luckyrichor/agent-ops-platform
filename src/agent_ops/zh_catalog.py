"""S3 前置 a：确定性中文字段生成与检索词表（合成语料，非真实商品数据）。

固定种子、纯函数生成：同 id 输入必得同 title_zh/description_zh。
商品类目（Olist 葡语名）与中文标题/描述特征一一对应，禁止 id%5 轮换词。
中文检索词表 = 类目主词 + 修饰词，查询用最长匹配切分。
"""
from __future__ import annotations

import hashlib
from typing import Any

# 类目 id（与 commerce.categories 一致）→ 中文主词与特征词表。
# 特征词同时进入 description_zh，供“主词+修饰词”查询命中。
CATEGORY_ZH: dict[int, dict[str, Any]] = {
    1: {"head": "农用喷壶", "features": ["加厚耐用", "大容量", "防滴漏"]},
    2: {"head": "坚果零食", "features": ["独立小包", "原味", "混合装"]},
    3: {"head": "果汁饮料", "features": ["低糖", "冷藏鲜榨", "整箱装"]},
    4: {"head": "手工画材", "features": ["水彩", "便携套装", "初学"]},
    5: {"head": "手工艺品", "features": ["手工编织", "家居摆件", "创意"]},
    6: {"head": "派对用品", "features": ["一次性", "彩色", "主题套装"]},
    7: {"head": "圣诞装饰", "features": ["LED灯串", "挂饰", "室内"]},
    8: {"head": "蓝牙耳机", "features": ["无线", "降噪", "长续航"]},
    9: {"head": "车载充电器", "features": ["快充", "双口", "迷你"]},
    10: {"head": "婴儿奶瓶", "features": ["防胀气", "玻璃", "宽口径"]},
    11: {"head": "瓶装水", "features": ["天然矿泉", "整箱", "小瓶装"]},
    12: {"head": "护肤套装", "features": ["保湿", "敏感肌", "礼盒"]},
    13: {"head": "儿童积木", "features": ["大颗粒", "益智", "环保塑料"]},
    14: {"head": "床品四件套", "features": ["纯棉", "简约", "双面可用"]},
    15: {"head": "电热毯", "features": ["定时", "双控", "安全断电"]},
    16: {"head": "暖手宝", "features": ["充电", "防爆", "便携"]},
    17: {"head": "电钻", "features": ["锂电", "家用", "多档扭矩"]},
    18: {"head": "音乐光盘", "features": ["经典专辑", "正版", "珍藏"]},
    19: {"head": "相机三脚架", "features": ["铝合金", "便携", "云台"]},
    20: {"head": "空调扇", "features": ["遥控", "静音", "加水冷风"]},
    21: {"head": "游戏手柄", "features": ["无线", "震动", "兼容PC"]},
    22: {"head": "安全帽", "features": ["工地", "透气", "可调节"]},
    23: {"head": "工具箱", "features": ["家用组合", "手提", "五金套装"]},
    24: {"head": "LED手电筒", "features": ["强光", "充电", "防水"]},
    25: {"head": "园艺剪刀", "features": ["修枝", "防滑手柄", "不锈钢刀头"]},
    26: {"head": "防护手套", "features": ["耐磨", "防割", "劳保"]},
    27: {"head": "创意小物", "features": ["桌面摆件", "新奇", "礼品"]},
    28: {"head": "蓝光碟", "features": ["正版", "高清", "收藏版"]},
    29: {"head": "洗碗机", "features": ["台式", "烘干", "家用小型"]},
    30: {"head": "微波炉", "features": ["转盘式", "平板式", "20升"]},
    31: {"head": "智能手表", "features": ["心率监测", "防水", "长续航"]},
    32: {"head": "电水壶", "features": ["304不锈钢", "自动断电", "大容量"]},
    33: {"head": "瑜伽垫", "features": ["防滑", "加厚", "环保TPE"]},
    34: {"head": "女士手提包", "features": ["真皮", "大容量", "通勤"]},
    35: {"head": "跑步鞋", "features": ["透气网面", "缓震", "轻便"]},
    36: {"head": "运动短裤", "features": ["速干", "透气", "健身"]},
    37: {"head": "连衣裙", "features": ["雪纺", "收腰", "夏季"]},
    38: {"head": "儿童T恤", "features": ["纯棉", "卡通印花", "透气"]},
    39: {"head": "男士衬衫", "features": ["免烫", "商务", "长袖"]},
    40: {"head": "泳衣", "features": ["速干", "防晒", "连体"]},
    41: {"head": "浇水壶", "features": ["长嘴", "家用", "加厚塑料"]},
    42: {"head": "仿真花束", "features": ["绢布", "家居装饰", "免打理"]},
    43: {"head": "婴儿纸尿裤", "features": ["超薄透气", "夜用", "L码"]},
    44: {"head": "打包胶带", "features": ["高粘", "加宽", "封箱"]},
    45: {"head": "无线鼠标", "features": ["静音", "可充电", "人体工学"]},
    46: {"head": "尤克里里", "features": ["初学", "21寸", "单板"]},
    47: {"head": "烘焙模具", "features": ["不粘", "烤箱用", "硅胶"]},
    48: {"head": "进口小说", "features": ["精装", "原著翻译", "畅销"]},
    49: {"head": "科普图书", "features": ["彩图版", "少儿百科", "正版"]},
    50: {"head": "技术教材", "features": ["第5版", "高校用书", "配套习题"]},
    51: {"head": "行李箱", "features": ["万向轮", "20寸", "密码锁"]},
    52: {"head": "百货杂货", "features": ["日用", "组合装", "实惠"]},
    53: {"head": "乳胶床垫", "features": ["天然乳胶", "护脊", "1.8米"]},
    54: {"head": "餐桌椅", "features": ["实木", "四椅一桌", "小户型"]},
    55: {"head": "装饰画", "features": ["客厅", "北欧风格", "带框"]},
    56: {"head": "电脑椅", "features": ["人体工学", "升降", "腰托"]},
    57: {"head": "衣柜", "features": ["简易组装", "两门", "收纳强"]},
    58: {"head": "沙发", "features": ["布艺", "小户型", "可拆洗"]},
    59: {"head": "电子琴", "features": ["61键", "初学", "带琴架"]},
    60: {"head": "中性笔", "features": ["0.5mm", "顺滑书写", "12支装"]},
    61: {"head": "电竞主机", "features": ["高配", "水冷", "独显"]},
    62: {"head": "笔记本电脑", "features": ["轻薄", "16G内存", "办公"]},
    63: {"head": "淡香水", "features": ["持久", "清新", "礼盒装"]},
    64: {"head": "猫粮", "features": ["成猫", "深海鱼配方", "2kg装"]},
    65: {"head": "咖啡机", "features": ["意式半自动", "打奶泡", "家用"]},
    66: {"head": "破壁机", "features": ["低音", "加热", "1.75L"]},
    67: {"head": "机械手表", "features": ["全自动机芯", "防水", "钢带"]},
    68: {"head": "延保服务", "features": ["一年期", "全国联保", "电子凭证"]},
    69: {"head": "反光背心", "features": ["夜间作业", "透气网布", "反光条"]},
    70: {"head": "平板电脑", "features": ["11英寸", "学习办公", "大电池"]},
    71: {"head": "手机壳", "features": ["防摔", "透明", "液态硅胶"]},
    72: {"head": "无绳电话", "features": ["座机", "大音量", "老人用"]},
    73: {"head": "其他商品", "features": ["综合", "实用", "随机发货"]},
    74: {"head": "保温杯", "features": ["304不锈钢", "大容量", "便携"]},
}

BRANDS = ["晨露", "云杉", "匠造", "悦享", "初品", "简叶", "优工", "臻选"]
SCENES = ["家用", "办公", "出行", "宿舍", "礼赠"]


def _pick(words: list[str], product_id: int, salt: str) -> str:
    digest = hashlib.sha256(f"{product_id}:{salt}".encode()).digest()
    return words[digest[0] % len(words)]


def title_zh(product_id: int, category_id: int) -> str:
    """确定性中文标题：主词与类目一致，型号由 id 派生。"""
    spec = CATEGORY_ZH[category_id]
    return f"{spec['head']} {_pick(BRANDS, product_id, 'brand')}型号{product_id}"


def description_zh(product_id: int, category_id: int) -> str:
    """确定性中文描述：类目特征词组合，供修饰词查询与相关性判定。"""
    spec = CATEGORY_ZH[category_id]
    first = _pick(spec["features"], product_id, "feat1")
    second = _pick(spec["features"], product_id, "feat2")
    if second == first:
        second = spec["features"][(spec["features"].index(first) + 1) % len(spec["features"])]
    return (f"{_pick(BRANDS, product_id, 'brand')}出品{spec['head']}，"
            f"{first}，{second}，{_pick(SCENES, product_id, 'scene')}适用")


# 检索词表：全部类目主词 + 修饰词（各主词特征词的并集）。
VOCAB: frozenset[str] = frozenset(
    {spec["head"] for spec in CATEGORY_ZH.values()}
    | {feature for spec in CATEGORY_ZH.values() for feature in spec["features"]}
)


def segment(query: str) -> list[str]:
    """最长匹配切分中文查询；未登录片段按原样保留为独立词。"""
    terms: list[str] = []
    index = 0
    while index < len(query):
        matched: str | None = None
        for length in range(min(6, len(query) - index), 1, -1):
            candidate = query[index:index + length]
            if candidate in VOCAB:
                matched = candidate
                break
        if matched is None:
            matched = query[index]
            index += 1
        else:
            index += len(matched)
        if matched.strip() and matched not in terms:
            terms.append(matched)
    return terms
