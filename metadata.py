"""
元数据管理模块

职责：
- 创建和维护元数据表
- 记录每个业务表的名称、字段、行数等信息
- 提供查询接口，供 list_tables 工具调用

与原 MetadataManager.py 的区别：
- 移除了 table_type 字段及其相关方法（get_target_tables）
- 新增 list_all_tables() 方法，返回所有表信息（LLM 自行语义判断相关性）
- fields_info 简化为 columns（JSON 数组，存储字段名列表）
- 新增 row_count 字段，方便 LLM 了解数据规模
"""

import sqlite3 # Python内置的轻量级数据库操作库，用来操作本地SQLite数据库
import json # 来把字典（字段映射）转成字符串存到数据库，或从数据库读取后转回字典
from datetime import datetime # 用来生成元数据的创建时间
from typing import List, Dict # 导入类型注解：指定函数参数/返回值的类型，让代码更易读、易维护（新手可先忽略，不影响功能）


class MetadataManager:
    """元数据管理器：负责业务表元数据的增删查"""

    def __init__(self, db_path: str): #  __init__方法：类的构造函数，创建实例时自动执行。参数db_path：数据库文件路径
        """
        初始化元数据管理器，自动创建元数据表。

        Args:
            db_path: SQLite 数据库文件路径
        """

        '''
        : str为类型注解，告诉开发者 / 编辑器，db_path 这个参数应该传入字符串类型的值（是 “提示”，不是 “强制限制”）。
            # 无类型注解（新手初期也可以这么写，不影响功能）
            def __init__(self, db_path):
            # 有类型注解（更规范，团队协作/大型项目必备）
            def __init__(self, db_path: str):
        '''
        self.db_path = db_path # 把传入的数据库文件路径保存为实例属性，后续所有方法都能用到
        self.metadata_table = "metadata_table" # 定义元数据表的表名，固定为"metadata_table"，后续操作这个表都用这个名字
        # 强化校验：调用_init_metadata_table方法创建元数据表
        # 如果创建失败（返回False），直接抛出异常，终止实例化（避免后续操作无意义）
        if not self._init_metadata_table():
            # self._init_metadata_table () 的返回值不是 0/1，是布尔值 True/False
            raise RuntimeError("元数据表创建失败，初始化终止") # raise 是 Python 里 “主动抛出异常” 的关键字。raise 就是手动让程序报错，并停止运行，还能自定义报错信息。
        
        

    def _connect(self) -> sqlite3.Connection:
        """创建数据库连接:封装数据库连接逻辑，避免重复写连接代码，下划线开头表示 “内部方法”（建议只在类内部调用）"""
        '''
        -> 是 Python 的 类型注解,不是执行代码，Python 解释器运行时会完全忽略它。-> 读作 “返回”，是类型注解的语法符号，用来告诉开发者（或编辑器）：这个函数 / 方法执行后，会返回什么类型的结果。
        sqlite3.Connection 不是 “函数”，而是 sqlite3 库中定义的一个类（Class），表示 “SQLite 数据库连接对象”
        '''
        conn = sqlite3.connect(self.db_path)
        '''
        调用 sqlite3 库的 connect() 函数，传入数据库文件路径（self.db_path），
        最终返回一个 sqlite3.Connection 类的实例对象（赋值给 conn），这个 conn 就是 “数据库连接”，后续所有操作（执行 SQL、创建游标）都要通过它。
        '''
        conn.row_factory = sqlite3.Row # 设置查询结果的格式：支持按字段名取值（如 row["business_table_name"]）
        '''
        如果不写 conn.row_factory = sqlite3.Row，执行查询后，每行结果是元组（tuple），只能按索引取值.
        设置 conn.row_factory = sqlite3.Row 后,结果的每行从 “元组” 改成 sqlite3.Row 类型的对象，这个对象支持按字段名取值（就像字典）：
        '''
        return conn # 这里返回的conn必须是sqlite3.Connection类型的对象（否则只是注解和实际不符，不报错）

    def _init_metadata_table(self):
        """创建元数据表（如果不存在）"""
        conn = None # 初始化数据库连接变量conn为None
        try: # try块：Python 异常处理的核心，包裹可能抛出异常的代码，如果代码执行出错，会跳转到except块处理。
            conn = self._connect()
            cursor = conn.cursor() # 通过数据库连接对象conn创建游标对象cursor，游标是 SQLite 执行 SQL 语句的核心工具，所有 SQL 操作都需要通过游标来执行。
            '''
            游标，通俗的解释就是"游动的标志"，这是数据库中一个很重要的概念。有时候，我们执行一条查询语句的时候，往往会得到N条返回结果，执行sql语句取出这些返回结果的接口(起始点)，就是游标。沿着这个游标，我们可以一次取出一行记录。
            当不使用游标功能，我们去执行select *from student where age>20;这条语句的时候，如果有1000条返回结果，系统会一次性将1000条记录返回到界面中，你没有选择，也不能做其他操作。
            当我们开启了游标功能，再去执行这条语句的时候，系统会先帮你找到这些行，先给你存放起来，然后提供了一个游标接口。当你需要数据的时候，就借助这个游标去一行行的取出数据，你每取出一条记录，游标指针就朝前移动一次，一直到取完最后一行数据后。
            
            利用python连接数据库，经常会使用游标功能:
            我们以python连接mysq1数据库来说明使用游标的好处。
            当我们使用python连接mysq1的时候，那么python就相当于是mysq1服务器的个客户端，我们利用python这个client去操纵mysql的server。
            在pymysq1中操作数据库，就是使用游标这种方式来获取表中的数据。

            使用游标的操作步骤:
            首先，使用pymysql连接上mysql数据库，得到一个数据库对象conn。
            然后，我们必须要开启数据库中的游标功能，得到一个游标对象。
            接着，使用游标对象中的execute()方法，去执行某个SQL语句，系统会根据你的SQL语句，找到这些匹配行，给你存储起来，而不是一次性的打印到频幕上。当你什么时候需要这个结果中的数据的时候，你就去获取它。
            最后，就是获取结果集中的数据了，这里有两种方法获取结果集中的数据。一个是fetchone()，该方法一次获取一条记录，每一条记录是一个元组形式的数据，每获取一条记录游标会往前移动一格，等待获取下一条记录；一个是fetchall()方法，能够一次性的获取所有的数据，该方法返回的是一个元组列表。
            当完成所有操作后，记得断开数据库的连接，释放资源。

            参考：https://blog.csdn.net/n_fly/article/details/118575596
            '''
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS `{self.metadata_table}` (
                    `table_id` INTEGER PRIMARY KEY AUTOINCREMENT,
                    `table_name` TEXT NOT NULL UNIQUE,
                    `original_sheet_name` TEXT NOT NULL,
                    `columns` TEXT NOT NULL,
                    `row_count` INTEGER DEFAULT 0,
                    `source_file` TEXT NOT NULL,
                    `created_at` DATETIME NOT NULL
                )
            """)
            # 定义了 7 个字段，涵盖 “table_id主键 ID、table_name业务表名、original_sheet_name原始工作表名、columns字段名列表、row_count数据行数、source_file源文件、created_at创建时间”，且通过NOT NULL/UNIQUE保证数据完整性
            '''注意：
            `table_id` INTEGER PRIMARY KEY AUTOINCREMENT：AUTOINCREMENT（自增）：当你向表中插入新记录时，SQLite 会自动为table_id分配一个唯一的整数值（1、2、3…），无需手动指定；PRIMARY KEY（主键）：数据库会保证这个字段的唯一性，不需要调用方关心；
            TEXT NOT NULL UNIQUE   TEXT：字段类型为文本（字符串）；NOT NULL：字段值不能为空（必须填值，否则插入数据报错）；UNIQUE：字段值必须唯一（比如不能有两个 “学生表” 的记录，避免重复）。
            TEXT NOT NULL	字段约束：文本类型、非空（必须填值）。
            '''
            conn.commit() # 提交数据库事务：SQLite 默认开启事务，执行建表语句后需要调用commit()确认修改，否则建表操作不会生效.
            return True
        except sqlite3.Error as e:
            print(f"❌ 创建元数据表失败：{str(e)}")
            return False
        finally:
            if conn:
                conn.close()
        
    def record_table(self, table_name: str, original_sheet_name: str, columns: List[str], row_count: int, source_file: str) -> bool:
        """
        记录或更新一张表的元数据。
        使用 INSERT OR REPLACE：如果表名已存在（UNIQUE 约束），
        则更新元数据（适用于重新导入同一个 Excel 的场景）。
        Args:
            table_name:          数据库中的表名
            original_sheet_name: Excel 原始 Sheet 名
            columns:             字段名列表
            row_count:           数据行数
            source_file:         源 Excel 文件路径
        Returns:
            True 表示成功，False 表示失败

        注注注：table_id是自增主键，SQLite 自动维护，不需要我们手动插入。新增记录时 SQLite 自动分配数字，不用你传值**。
        """
        conn = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            # 这是一条 SQLite 的**插入或替换语句**，用来往元数据表写入一行元数据。
            insert_sql = f'INSERT OR REPLACE INTO `{self.metadata_table}` (`table_name`, `original_sheet_name`, `columns`, `row_count`, `source_file`, `created_at`) VALUES (?, ?, ?, ?, ?, ?)'
            '''
            `INSERT`：插入新数据；`OR REPLACE`：SQLite 特有语法。
            逻辑：尝试插入这一行。如果插入时触发 **UNIQUE / PRIMARY KEY 唯一约束冲突**，就：1. 先把已经冲突的旧记录删掉，2. 再把本次新的数据插入进去。
            `VALUES`后面跟要填入的值。这里 6 个问号 `?`，叫参数占位符，一共 6 个，和上面 6 个字段一一对应。cursor.execute() 方法会把后面传入的元组里的值，依次替换掉问号，最终形成完整的 SQL 语句。
            '''
            cursor.execute(
                insert_sql,
                (
                    table_name,
                    original_sheet_name,
                    json.dumps(columns, ensure_ascii=False),
                    row_count,
                    source_file,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), # 尾随逗号
                ), # 尾随逗号
            )
            '''
            Python 里这个末尾逗号不是语法错误，是允许的
            元组(a,b,c,)等价于(a,b,c)。
            最后一项后面加逗号，叫尾随逗号 (trailing comma)。
            '''

            '''
            JSON 标准里，JSON 有两种合法根节点：
            1. 对象：`{ ... }` 大括号，键值对
            2. 数组：`[ ... ]` 方括号，列表
            JSON 字符串，不一定必须是键值对大括号，数组也是合法 JSON。

            json.dumps()主要就是用来处理 Python 的字典 dict 和列表 list
            json.dumps(columns, ensure_ascii=False)作用：把 Python 的列表对象 `columns`，转换成 JSON 格式字符串，存入 SQLite 的 TEXT 字段
            ensure_ascii=False：关键参数，保证中文等非 ASCII 字符正常存储

            #datetime.now()：获取当前本地时间。strftime("%Y-%m-%d %H:%M:%S")：将时间格式化为年-月-日 时:分:秒的字符串
            '''
            conn.commit()
            print(f"✅ 元数据记录成功，table_name={table_name}")
            return True
        except sqlite3.Error as e:
            print(f"❌ [元数据写入失败] {e}")
            return False
        finally:
            if conn:
                conn.close()

    def list_all_tables(self) -> List[Dict]: # 返回字典列表
        """
        查询所有表的元数据。

        LLM 通过此方法获取所有表信息，然后自行语义判断哪些表与用户需求相关。
        不再需要 table_type 做预过滤。

        Returns:
            字典列表，每个字典包含：
            - table_name:          表名
            - original_sheet_name: 原始 Sheet 名
            - columns:             字段名列表（已从 JSON 解析）
            - row_count:           数据行数
            - source_file:         源文件路径
            - created_at:          创建时间
        """
        conn = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            select_sql = f'SELECT `table_name`, `original_sheet_name`, `columns`, `row_count`, `source_file`, `created_at` FROM `{self.metadata_table}` ORDER BY `table_id`' # 升序排列
            cursor.execute(select_sql)
            rows = cursor.fetchall() # 获取查询结果的所有行，取出本次 SQL 查询返回的全部结果行，一次性全部拿出来，变成 Python 里面的**元组列表**，列表的每个元素是元组
            # rows不是sqlite3.Row对象，它的里面每一项是sqlite3.Row对象。
            result = []
            for row in rows: # row 是 sqlite3.Row 对象
                row_dict = dict(row) # 转为真正Python字典，把 Row 拷贝成普通 Python 字典，方便上层交给 LLM、序列化，这才是转换的真正目的，不是为了 “能按字段名取值”。仅仅做对象拷贝，把 sqlite3.Row 的键值对原封不动复制到普通 Python 字典。它只做一层对象转换，完全不解析字段里面的内容。
                try:
                    row_dict["columns"] = json.loads(row_dict["columns"])
                    # 从数据库读出来的columns是JSON 格式字符串（存入的时候用的json.dumps()）；json.loads()把这个字符串解析还原成 Python 的列表对象，再重新赋值给字典里的columns。
                except json.JSONDecodeError:
                    row_dict["columns"] = []
                result.append(row_dict)
            print(f"✅ 查询元数据表成功，共拿到 {len(result)} 条表元数据")
            return result
        except sqlite3.Error as e:
            print(f"❌ [元数据查询失败] {e}")
            return []
        finally:
            if conn:
                conn.close()
