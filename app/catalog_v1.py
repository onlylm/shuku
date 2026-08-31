"""八大类目录规则 v1。迁移也使用此版本；以后改规则请新增版本，不改写历史。

仅规范来源分类名称，不推测书名、ISBN 或版权。第一组名称是宽泛别名，
第二组名称保留为二级分类；只做完整名称匹配。
"""
import re
import unicodedata

LAYOUT_KEY = "catalog_layout"
GROUPS = (
    ("literature", "文学小说", ("文学", "小说"), ()),
    ("history", "历史传记", ("历史文化", "历史人文", "历史地理"), ("历史", "人物传记", "中国历史", "世界历史", "军事史", "地理")),
    ("society", "哲学社科", ("社科人文",), ("社会科学", "社会科学总论", "哲学思想", "哲学、宗教", "哲学", "宗教", "政治、法律", "政治法律", "军事", "马克思主义、列宁主义、毛泽东思想、邓小平理论")),
    ("business", "经济管理", ("商业管理", "经济金融"), ("经济", "金融", "管理", "企业管理", "投资理财", "市场营销")),
    ("science", "科学技术", ("科技",), ("计算机互联网", "计算机、互联网", "计算机", "编程开发", "工业技术", "科学科普", "自然科学总论", "数理科学和化学", "天文学、地球科学", "生物科学", "医药、卫生", "农业科学", "交通运输", "航空、航天", "环境科学、安全科学")),
    ("art", "艺术设计", (), ("艺术", "绘画", "摄影", "音乐", "设计")),
    ("life", "生活成长", (), ("心理学", "心理", "生活实用", "个人成长", "自我成长", "健康生活", "旅行")),
    ("education", "教育少儿", ("教育学习",), ("教育", "少儿读物", "儿童读物", "语言、文字", "语言文字", "语言学习", "文化、科学、教育、体育", "公开课程", "考试")),
)


def normalized_name(value):
    return re.sub(r"[\s、，,·/＆&-]+", "", unicodedata.normalize("NFKC", str(value or "")).strip()).casefold()


def source_group(name):
    key = normalized_name(name)
    for code, title, broad, specific in GROUPS:
        if key in {normalized_name(x) for x in (title, *broad)}:
            return code, True
        if key in {normalized_name(x) for x in specific}:
            return code, False
    return None
