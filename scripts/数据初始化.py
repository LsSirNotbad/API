# -*- coding: utf-8 -*-
"""
数据初始化脚本：一键完成数据库维护 + 基础数据灌入
1) 维护：按模型定义给已存在的表补表注释和字段注释（不影响数据，可重复运行）
2) 初始化：向各数据表灌入基础示例数据，已有数据的表自动跳过
用法：python scripts/数据初始化.py
"""
import os
import sys
import time

# 脚本位于 scripts/ 子目录，需要把项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.dialects import mysql
from main import (
    SessionLocal, Base, engine,
    KnowledgeDB, RetortDB, NameDB, LocalJokeDB, HotNewsDB, DailyImageDB,
    IdiomDB, PoemDB, CopywritingDB, WordWisdomDB, LoveTalkDB, PoisonSoupDB,
    RiddleDB, XiehouyuDB, TwisterDB, AnswerBookDB, HistoryTodayDB,
)

# 确保所有表已创建
Base.metadata.create_all(bind=engine)

NOW = time.strftime("%Y-%m-%d %H:%M:%S")

DATA = {
    "knowledge": (KnowledgeDB, [
        {"category": "IT", "title": "HTTP状态码200", "content": "200 OK 表示请求成功，服务器已成功处理请求并返回数据。"},
        {"category": "IT", "title": "HTTP状态码404", "content": "404 Not Found 表示请求的资源不存在，常见于链接错误或资源被删除。"},
        {"category": "IT", "title": "HTTP状态码500", "content": "500 Internal Server Error 表示服务器内部错误，通常是后端代码抛异常。"},
        {"category": "IT", "title": "MySQL索引", "content": "索引可以大幅提高查询速度，但会降低写入速度，不要在低基数列上建索引。"},
        {"category": "IT", "title": "Python GIL", "content": "GIL（全局解释器锁）使CPython同一时刻只有一个线程执行字节码，多线程适合IO密集型，不适合CPU密集型。"},
        {"category": "生活", "title": "隔夜水能喝吗", "content": "隔夜的白开水可以喝，亚硝酸盐含量极低，远低于国家标准，不会致癌。"},
        {"category": "生活", "title": "冰箱除霜技巧", "content": "放一碗热水进冷冻室关门10分钟，霜层会快速松动脱落，省时省力。"},
        {"category": "生活", "title": "蚊子叮咬止痒", "content": "用肥皂水涂抹叮咬处可中和蚁酸，缓解瘙痒；也可以用冰敷。"},
        {"category": "历史", "title": "秦始皇统一六国", "content": "公元前221年，秦王嬴政灭齐，完成统一，建立中国历史上第一个中央集权王朝。"},
        {"category": "历史", "title": "四大发明", "content": "造纸术、指南针、火药、活字印刷术，是中国古代对世界影响深远的四项发明。"},
        {"category": "文学", "title": "唐宋八大家", "content": "韩愈、柳宗元、欧阳修、苏洵、苏轼、苏辙、王安石、曾巩。"},
        {"category": "文学", "title": "四大名著", "content": "《红楼梦》《西游记》《三国演义》《水浒传》。"},
    ]),
    "retort": (RetortDB, [
        {"level": "mild", "content": "你说的很有道理，下次别说了。"},
        {"level": "mild", "content": "我尊重你的观点，但保留我的智商。"},
        {"level": "mild", "content": "你先别急，急也解决不了问题。"},
        {"level": "sharp", "content": "脑子是个日用品，希望你不要把它当成装饰品。"},
        {"level": "sharp", "content": "你的逻辑很独特，独特到自成一派。"},
        {"level": "sharp", "content": "我不跟你争，我怕赢了胜之不武。"},
        {"level": "funny", "content": "你这是在跟我讲道理，还是在讲脱口秀？"},
        {"level": "funny", "content": "听君一席话，如听一席话。"},
        {"level": "funny", "content": "你说得对，我竟无法反驳，只能鼓掌。"},
        {"level": "funny", "content": "这波操作我给满分，因为找不到扣分的地方，全是扣分的。"},
    ]),
    "name": (NameDB, [
        {"type": "boy", "text": "沐辰"}, {"type": "boy", "text": "亦辰"}, {"type": "boy", "text": "宇航"},
        {"type": "boy", "text": "浩然"}, {"type": "boy", "text": "子墨"}, {"type": "boy", "text": "景行"},
        {"type": "girl", "text": "若曦"}, {"type": "girl", "text": "诗涵"}, {"type": "girl", "text": "欣怡"},
        {"type": "girl", "text": "雨桐"}, {"type": "girl", "text": "梦瑶"}, {"type": "girl", "text": "清婉"},
        {"type": "net", "text": "星野晚风"}, {"type": "net", "text": "半糖去冰"}, {"type": "net", "text": "人间小清醒"},
        {"type": "net", "text": "骑驴追流星"}, {"type": "net", "text": "偷喝汽水"}, {"type": "net", "text": "雾里看花落"},
        {"type": "shop", "text": "拾光小馆"}, {"type": "shop", "text": "南巷旧茶"}, {"type": "shop", "text": "半山书局"},
        {"type": "shop", "text": "云边有个小卖部"}, {"type": "shop", "text": "一隅咖啡"}, {"type": "shop", "text": "青禾花坊"},
    ]),
    "joke": (LocalJokeDB, [
        {"content": "有一天，螃蟹出门散步不小心撞到了泥鳅。泥鳅很生气：'你是不是瞎啊？'螃蟹很委屈：'不是啊，我是螃蟹！'"},
        {"content": "老师问小明：'如果你有一百块钱，借给同学五十，还剩多少？'小明：'一百。'老师：'你不懂数学。'小明：'你不懂我同学。'"},
        {"content": "医生对病人说：'你需要多运动。'病人：'我每天都在跑步啊！'医生：'在哪跑？'病人：'在脑子里跑，一天能跑好几圈。'"},
        {"content": "面试官：'你的优点是什么？'答：'我做事从不拖延。'面试官：'那你的缺点呢？'答：'这件事我明天再告诉你。'"},
        {"content": "我问朋友：'你减肥成功的秘诀是什么？'他说：'坚持。'我说：'坚持运动？'他说：'坚持不称体重。'"},
        {"content": "程序员最讨厌四件事：写注释、写文档、别人不写注释、别人不写文档。"},
        {"content": "小时候总骗爸妈没钱了，长大后总骗爸妈还有钱。"},
        {"content": "有人问我为什么不谈恋爱，我说：'我这么可爱，谈恋爱多影响我行走江湖。'"},
        {"content": "老板说：'公司就是你的家。'我：'那我能在家躺着吗？'"},
        {"content": "减肥这种事，三分靠练，七分靠吃，剩下九十分靠天赋。"},
    ]),
    "hot": (HotNewsDB, [
        {"title": "国产大模型再升级，推理能力大幅提升", "url": "https://example.com/news/1", "add_time": NOW},
        {"title": "新能源汽车销量创历史新高", "url": "https://example.com/news/2", "add_time": NOW},
        {"title": "全国多地迎来入秋首场降温", "url": "https://example.com/news/3", "add_time": NOW},
        {"title": "我国成功发射新一颗通信卫星", "url": "https://example.com/news/4", "add_time": NOW},
        {"title": "国庆假期旅游市场持续火热", "url": "https://example.com/news/5", "add_time": NOW},
    ]),
    "image": (DailyImageDB, [
        {"desc": "雪山日出", "img_url": "https://picsum.photos/id/1018/1920/1080"},
        {"desc": "森林小径", "img_url": "https://picsum.photos/id/1015/1920/1080"},
        {"desc": "海边黄昏", "img_url": "https://picsum.photos/id/1016/1920/1080"},
        {"desc": "城市夜景", "img_url": "https://picsum.photos/id/1029/1920/1080"},
        {"desc": "草原风光", "img_url": "https://picsum.photos/id/1043/1920/1080"},
        {"desc": "星空银河", "img_url": "https://picsum.photos/id/1044/1920/1080"},
    ]),
    "idiom": (IdiomDB, [
        {"word": "画蛇添足", "explain": "比喻做了多余的事，非但无益，反而不合适。"},
        {"word": "守株待兔", "explain": "比喻不主动努力，心存侥幸，希望得到意外收获。"},
        {"word": "亡羊补牢", "explain": "比喻出了问题以后想办法补救，可以防止继续受损失。"},
        {"word": "井底之蛙", "explain": "比喻见识狭窄的人。"},
        {"word": "对牛弹琴", "explain": "比喻对不懂道理的人讲道理，也用来讥笑说话不看对象。"},
        {"word": "刻舟求剑", "explain": "比喻拘泥不知变通，不懂得根据实际情况处理问题。"},
        {"word": "掩耳盗铃", "explain": "比喻自己欺骗自己，明明掩盖不住的事情偏要想法子掩盖。"},
        {"word": "杯弓蛇影", "explain": "比喻因疑神疑鬼而引起恐惧。"},
        {"word": "愚公移山", "explain": "比喻坚持不懈地改造自然和坚定不移地进行斗争。"},
        {"word": "滥竽充数", "explain": "比喻无本领的冒充有本领，次货冒充好货。"},
        {"word": "卧薪尝胆", "explain": "形容人刻苦自励，发奋图强。"},
        {"word": "破釜沉舟", "explain": "比喻下决心不顾一切地干到底。"},
    ]),
    "poem": (PoemDB, [
        {"title": "静夜思", "author": "李白", "dynasty": "唐", "content": "床前明月光，疑是地上霜。举头望明月，低头思故乡。"},
        {"title": "春晓", "author": "孟浩然", "dynasty": "唐", "content": "春眠不觉晓，处处闻啼鸟。夜来风雨声，花落知多少。"},
        {"title": "登鹳雀楼", "author": "王之涣", "dynasty": "唐", "content": "白日依山尽，黄河入海流。欲穷千里目，更上一层楼。"},
        {"title": "相思", "author": "王维", "dynasty": "唐", "content": "红豆生南国，春来发几枝。愿君多采撷，此物最相思。"},
        {"title": "江雪", "author": "柳宗元", "dynasty": "唐", "content": "千山鸟飞绝，万径人踪灭。孤舟蓑笠翁，独钓寒江雪。"},
        {"title": "悯农", "author": "李绅", "dynasty": "唐", "content": "锄禾日当午，汗滴禾下土。谁知盘中餐，粒粒皆辛苦。"},
        {"title": "赋得古原草送别", "author": "白居易", "dynasty": "唐", "content": "离离原上草，一岁一枯荣。野火烧不尽，春风吹又生。"},
        {"title": "望庐山瀑布", "author": "李白", "dynasty": "唐", "content": "日照香炉生紫烟，遥看瀑布挂前川。飞流直下三千尺，疑是银河落九天。"},
        {"title": "水调歌头·明月几时有", "author": "苏轼", "dynasty": "宋", "content": "明月几时有？把酒问青天。不知天上宫阙，今夕是何年。"},
        {"title": "游子吟", "author": "孟郊", "dynasty": "唐", "content": "慈母手中线，游子身上衣。临行密密缝，意恐迟迟归。谁言寸草心，报得三春晖。"},
    ]),
    "copy": (CopywritingDB, [
        {"ctype": "朋友圈", "content": "生活明朗，万物可爱，人间值得，未来可期。"},
        {"ctype": "朋友圈", "content": "把日子过成诗，把平凡过成远方。"},
        {"ctype": "朋友圈", "content": "今日份快乐已到账，请查收。"},
        {"ctype": "朋友圈", "content": "山高路远，看世界，也找自己。"},
        {"ctype": "签名", "content": "保持热爱，奔赴山海。"},
        {"ctype": "签名", "content": "知足且上进，温柔而坚定。"},
        {"ctype": "签名", "content": "心之所向，素履以往。"},
        {"ctype": "签名", "content": "万事尽头，终将如意。"},
        {"ctype": "短视频", "content": "这大概就是青春最好的样子。"},
        {"ctype": "短视频", "content": "别眨眼，精彩马上开始。"},
        {"ctype": "短视频", "content": "生活不止眼前的苟且，还有镜头里的美好。"},
    ]),
    "wisdom": (WordWisdomDB, [
        {"tag": "励志", "content": "你只管努力，剩下的交给时间。"},
        {"tag": "励志", "content": "乾坤未定，你我皆是黑马。"},
        {"tag": "励志", "content": "星光不问赶路人，时光不负有心人。"},
        {"tag": "励志", "content": "没有白走的路，每一步都算数。"},
        {"tag": "治愈", "content": "愿你被这个世界温柔以待。"},
        {"tag": "治愈", "content": "慢慢来，谁还没有一个努力的过程。"},
        {"tag": "治愈", "content": "一切都会过去的，包括坏情绪。"},
        {"tag": "治愈", "content": "别慌，月亮也正在大海某处迷茫。"},
        {"tag": "清醒", "content": "及时止损，是成年人最高级的自律。"},
        {"tag": "清醒", "content": "你不优秀，认识谁都没用。"},
        {"tag": "清醒", "content": "人最大的敌人，是自己的懒惰。"},
        {"tag": "清醒", "content": "别把平台当本事，别把运气当实力。"},
    ]),
    "love": (LoveTalkDB, [
        {"content": "你知道我的缺点是什么吗？是缺点你。"},
        {"content": "最近手头有点紧，想借你的手牵牵。"},
        {"content": "你是哪里人？你是我的心上人。"},
        {"content": "我觉得你像一款游戏。什么游戏？我的世界。"},
        {"content": "你会弹吉他吗？不会。那你怎么拨动了我的心弦？"},
        {"content": "我一点不想你，但一点半想你了。"},
        {"content": "你近视吗？不近视啊。那你怎么看不出我喜欢你。"},
        {"content": "这是我的手背，这是我的脚背，你是我的宝贝。"},
        {"content": "你知道你和星星的区别吗？星星在天上，你在我心里。"},
        {"content": "我最近体重上升了，绝对没有偷吃，只是把你偷偷放在了心里。"},
    ]),
    "poison": (PoisonSoupDB, [
        {"content": "比你优秀的人还在努力，你努力有什么用。"},
        {"content": "世上无难事，只要肯放弃。"},
        {"content": "生活不止眼前的苟且，还有明天和后天的苟且。"},
        {"content": "谁说你没有毅力的？单身这件事你不就坚持了好多年。"},
        {"content": "你以为有钱人很快乐吗？他们的快乐你根本想象不到。"},
        {"content": "努力不一定成功，但不努力一定很轻松。"},
        {"content": "丑小鸭能变成白天鹅，不是因为它多努力，而是它爸妈本来就是白天鹅。"},
        {"content": "有时候你不努力一下，都不知道什么叫绝望。"},
        {"content": "人生就是六个字：怎么着都不行。"},
        {"content": "失败并不可怕，可怕的是你还相信这句话。"},
    ]),
    "riddle": (RiddleDB, [
        {"question": "什么东西越洗越脏？", "answer": "水"},
        {"question": "什么门永远关不上？", "answer": "球门"},
        {"question": "什么书在书店买不到？", "answer": "遗书（秘书）"},
        {"question": "什么东西有头无脚？", "answer": "钉子"},
        {"question": "小明的妈妈有三个孩子，老大叫大毛，老二叫二毛，老三叫什么？", "answer": "小明"},
        {"question": "什么东西越热越爱出来？", "answer": "汗"},
        {"question": "什么动物最容易摔倒？", "answer": "狐狸（因为狡猾/脚滑）"},
        {"question": "什么东西打破了才能吃？", "answer": "鸡蛋"},
        {"question": "什么车寸步难行？", "answer": "风车"},
        {"question": "用铁锤锤鸡蛋为什么锤不破？", "answer": "铁锤当然不会破"},
    ]),
    "xiehouyu": (XiehouyuDB, [
        {"riddle": "泥菩萨过河", "answer": "自身难保"},
        {"riddle": "外甥打灯笼", "answer": "照旧（照舅）"},
        {"riddle": "孔夫子搬家", "answer": "尽是输（书）"},
        {"riddle": "小葱拌豆腐", "answer": "一清二白"},
        {"riddle": "十五个吊桶打水", "answer": "七上八下"},
        {"riddle": "芝麻开花", "answer": "节节高"},
        {"riddle": "猫哭耗子", "answer": "假慈悲"},
        {"riddle": "竹篮打水", "answer": "一场空"},
        {"riddle": "狗拿耗子", "answer": "多管闲事"},
        {"riddle": "王婆卖瓜", "answer": "自卖自夸"},
        {"riddle": "八仙过海", "answer": "各显神通"},
        {"riddle": "擀面杖吹火", "answer": "一窍不通"},
    ]),
    "twister": (TwisterDB, [
        {"content": "八百标兵奔北坡，炮兵并排北边跑。炮兵怕把标兵碰，标兵怕碰炮兵炮。"},
        {"content": "四是四，十是十，十四是十四，四十是四十。"},
        {"content": "吃葡萄不吐葡萄皮，不吃葡萄倒吐葡萄皮。"},
        {"content": "黑化肥发灰会挥发，灰化肥挥发会发黑。"},
        {"content": "牛郎恋刘娘，刘娘恋牛郎，牛郎年年念刘娘，刘娘年年恋牛郎。"},
        {"content": "红凤凰，粉凤凰，红粉凤凰花凤凰。"},
        {"content": "哥挎瓜筐过宽沟，过沟筐漏瓜滚沟。"},
        {"content": "扁担长，板凳宽，扁担想绑在板凳上，板凳不让扁担绑在板凳上。"},
    ]),
    "answer": (AnswerBookDB, [
        {"content": "是的，放手去做吧。"},
        {"content": "再等等，时机未到。"},
        {"content": "答案就在你心里。"},
        {"content": "不要犹豫。"},
        {"content": "这件事会带来好运。"},
        {"content": "顺其自然。"},
        {"content": "换个角度想想。"},
        {"content": "现在不是最好的时机。"},
        {"content": "坚持下去，会有转机。"},
        {"content": "听从内心的声音。"},
        {"content": "别想了，去做就对了。"},
        {"content": "先放一放，明天再说。"},
        {"content": "你早就知道答案了。"},
        {"content": "风险与机遇并存。"},
        {"content": "相信自己。"},
    ]),
    "history": (HistoryTodayDB, [
        {"month_day": "10-01", "year": "1949", "event": "中华人民共和国中央人民政府成立典礼（开国大典）在北京天安门广场举行。"},
        {"month_day": "10-08", "year": "1955", "event": "科学家钱学森冲破重重阻力，从美国回到祖国。"},
        {"month_day": "10-08", "year": "1998", "event": "哈勃太空望远镜发现迄今最遥远的星系。"},
        {"month_day": "01-01", "year": "1912", "event": "中华民国成立，孙中山就任临时大总统。"},
        {"month_day": "07-01", "year": "1997", "event": "香港回归祖国，中华人民共和国香港特别行政区成立。"},
        {"month_day": "12-25", "year": "1991", "event": "苏联解体，冷战结束。"},
        {"month_day": "04-24", "year": "1970", "event": "中国第一颗人造地球卫星'东方红一号'发射成功。"},
        {"month_day": "08-08", "year": "2008", "event": "第29届夏季奥林匹克运动会在北京开幕。"},
        {"month_day": "11-11", "year": "2009", "event": "首届'双十一'购物狂欢节举办。"},
        {"month_day": "05-12", "year": "2008", "event": "四川汶川发生里氏8.0级特大地震。"},
    ]),
}


def seed():
    db = SessionLocal()
    try:
        total_new = 0
        for name, (model, rows) in DATA.items():
            existing = db.query(model).count()
            if existing > 0:
                print(f"[跳过] {name} 表已有 {existing} 条数据，不重复插入")
                continue
            for row in rows:
                db.add(model(**row))
            db.commit()
            total_new += len(rows)
            print(f"[成功] {name} 表插入 {len(rows)} 条数据")
        print(f"\n初始化完成，本次共新增 {total_new} 条数据")
    finally:
        db.close()


# ---------------- 数据库维护（补表注释和字段注释） ----------------

def _quote_default(value: str) -> str:
    v = str(value)
    if v.upper().replace(" ", "") in ("CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()", "NOW()"):
        return "CURRENT_TIMESTAMP"
    return "'" + v.replace("'", "''") + "'"


def _model_default(col):
    """模型里声明的 server_default，用于补回库里丢失的默认值"""
    sd = getattr(col, "server_default", None)
    if sd is None or sd.arg is None:
        return None
    arg = sd.arg
    return str(getattr(arg, "text", arg))


def update_comments():
    """根据模型定义，ALTER 已存在的表，补表注释和字段注释"""
    dialect = mysql.dialect()
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            t_comment = table.comment or ""
            conn.exec_driver_sql(f"ALTER TABLE `{table.name}` COMMENT = %s", (t_comment,))

            rows = conn.exec_driver_sql(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (table.name,)).fetchall()
            info = {r[0]: r for r in rows}

            pk_rows = conn.exec_driver_sql(
                "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                "AND CONSTRAINT_NAME = 'PRIMARY'", (table.name,)).fetchall()
            pk_cols = {r[0] for r in pk_rows}

            for col in table.columns:
                meta = info.get(col.name)
                col_type = meta[1] if meta else col.type.compile(dialect=dialect)
                nullable = meta[2] if meta else ("NO" if col.primary_key else "YES")
                default = meta[3] if meta else None
                if default is None:
                    default = _model_default(col)
                extra = (meta[4] or "") if meta else ""

                parts = [f"`{col.name}`", col_type,
                         "NOT NULL" if (col.name in pk_cols or nullable == "NO") else "NULL"]
                if default is not None:
                    parts.append("DEFAULT " + _quote_default(default))
                if "auto_increment" in extra.lower():
                    parts.append("AUTO_INCREMENT")
                c_comment = (col.comment or "").replace("'", "''")
                parts.append(f"COMMENT '{c_comment}'")

                conn.exec_driver_sql(
                    f"ALTER TABLE `{table.name}` MODIFY COLUMN " + " ".join(parts))
            print(f"[注释] {table.name} 已更新")


if __name__ == "__main__":
    print("===== 第 1 步：数据库维护（补表/字段注释） =====")
    update_comments()
    print("\n===== 第 2 步：基础数据初始化 =====")
    seed()
    print("\n全部完成")
