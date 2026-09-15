"""
Excel 导入模块

职责：
- 读取多 Sheet Excel 文件
- 自动处理合并单元格
- 通过评分算法识别表头行
- 清洗字段名（处理非法字符）
- 过滤无效数据行（空行、重复表头）
- 创建 SQLite 业务表并插入数据
- 记录元数据

与原 ExcelToMultiDB.py 的区别：
- 移除了 _get_table_type()（交互式类型选择）和 _dedup_competition_name()（竞赛名称去重）
- 移除了 table_type 相关逻辑，表名直接使用清洗后的 Sheet 名
- import_excel() 返回结构化字典（而非 bool），包含每个表的导入详情
- 保留核心解析逻辑：合并单元格处理、表头识别评分、字段名清洗、数据行过滤
"""

import os
import re
import sqlite3
from typing import List, Tuple, Dict
from openpyxl import load_workbook # openpyxl 是处理 Excel 的核心库，支持读取合并单元格、多 Sheet
from metadata import MetadataManager


class ExcelImporter:
    """Excel 导入器：将多 Sheet Excel 文件导入 SQLite 数据库"""

    def __init__(self, db_path: str):
        """
        初始化 Excel 导入器

        Args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = db_path
        self.metadata_manager = MetadataManager(db_path) # 实例化元数据管理器，关联当前数据库
        '''
        内部会实例化MetadataManager(db_path)，而 MetadataManager 内部构造函数会执行：
        conn = sqlite3.connect(db_path)
        只要你调用 sqlite3.connect(xxx.db)，不管你有没有建表、有没有写数据，磁盘上就会直接创建这个 .db 文件
        '''


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

    # ========================================================
    # 表头识别逻辑（从原代码完整保留）
    # ========================================================

    def _is_title_row(self, row: List[str]) -> bool:
        """
        判断一行是否是需要过滤的行（大标题行 / 空行）。

        判定规则：
        - 空行 -> True
        - 所有有效单元格内容相同（合并单元格导致）-> True
        - 有效单元格数远小于总列数（跨列合并，<30%）-> True

        Returns:
            True 表示该行是标题行或空行，应跳过
        """
        valid_cells = [cell.strip() for cell in row if cell.strip()] # 从代表 Excel 一行的 row 列表中，提取所有 “非空且去除首尾空格” 的单元格内容，最终生成一个只包含有效内容的新列表。
        '''
        列表推导式的通用结构是：[处理后的元素 for 元素 in 可迭代对象 if 筛选条件]
        等价于：
        valid_cells = []
        for cell in row:
            cleaned_cell = cell.strip()
            if cleaned_cell:  
                valid_cells.append(cleaned_cell)
        '''

        if len(valid_cells) == 0:
            return True  # 空行

        # 规则1：所有有效单元格内容都相同（合并单元格导致）
        all_same = all(cell == valid_cells[0] for cell in valid_cells) # all_same也是布尔值True或False
        '''
        cell == valid_cells[0] 是一个布尔判断（真假判断），它的返回值只有两种：True和False

        all() 是 Python 内置函数，核心规则：
        接收一个可迭代对象（比如这里的生成器 cell == valid_cells[0] for cell in valid_cells）；
        只有当可迭代对象里所有元素都是 True 时，all() 返回 True；
        只要有一个元素是 False，all() 直接返回 False（“一票否决”）。
        '''

        # 规则2：有效单元格数远小于总列数（跨列合并）
        is_span = len(valid_cells) / len(row) < 0.3 if len(row) > 0 else True
        '''
        len(valid_cells) / len(row) < 0.3 也是一个布尔判断（真假判断）
        '''
        return all_same or is_span # 布尔逻辑 “或”（OR） 的判断：只要 all_same 和 is_span 中有任意一个为 True，整个表达式结果就是 True；只有两个都为 False 时，结果才是 False。


    def _find_header_row(self, all_data: List[List[str]], max_header_row: int = 10) -> Tuple[int, List[str]]:
        """
        纯通用表头识别：完全不依赖关键词，仅通过行特征判断（过滤大标题行），找到最可能是表头的行。
        :param all_data: 清洗后的二维列表（all_data），每行是一个列表
        :param max_header_row: 表头最多出现在前N行（默认前5行，可调整）
        :return: header_idx（表头行索引）、headers（有效表头字段）

        评分维度（与原代码一致）：
        - 文本占比（50%）：表头以文本为主，数据行多含数字
        - 字段完整度（30%）：表头通常是列数最全的行
        - 长度特征（20%）：表头字段名通常比数据短

        Returns:
            (header_idx, headers) — 表头行索引和有效表头字段列表组成的元组
            如果未找到，返回 (-1, [])
        """
        header_idx = -1
        headers = []
        best_score = 0  # 表头匹配得分（越高越可能是表头）

        # 只遍历前N行（表头几乎不会出现在数据中间）
        for idx, row in enumerate(all_data[:max_header_row]):
            # 过滤大标题行/空行
            if self._is_title_row(row):
                continue # Python 循环（for/while）中的关键字，作用是跳过当前循环的剩余代码，直接进入下一次循环迭代

            valid_cells = [cell.strip() for cell in row if cell.strip()]

            # ------------- 核心：计算行的「表头特征得分」-------------
            # 特征1：纯文本占比（表头以文本为主，数据行多含数字）
            text_count = 0 # 文本数
            number_count = 0 # 数字
            for cell in valid_cells:
                # 排除数字（含小数、负数、百分比、金额），比如这些-100、1,200、99.9%、￥5000经过清洗后都是纯数字，应该算作数字而非文本
                cell_clean = cell.replace(".", "").replace("-", "").replace("%", "").replace("￥", "").replace(",", "") # `str.replace(旧字符,新字符)`：把字符串里**所有旧字符替换成新字符**；这里第二个参数传空字符串`""` = **直接删掉这个字符**。链式调用，等价于依次做：1. 删掉小数点 `.` 2. 删掉负号 `-` 3. 删掉百分号 `%` 4. 删掉人民币符号 `￥` 5. 删掉千位分隔逗号 `,`
                if cell_clean.isdigit(): # 判断字符串的所有字符是不是都是阿拉伯数字
                    number_count += 1
                else:
                    text_count += 1
            text_ratio = text_count / len(valid_cells) if len(valid_cells) > 0 else 0 # 文本占比

            # 特征2：有效字段数占比
            field_ratio = len(valid_cells) / len(row) if len(row) > 0 else 0 # 有效字段数占比

            # 特征3：字段长度特征（表头字段名通常较短）
            avg_length = sum(len(cell) for cell in valid_cells) / len(valid_cells) if len(valid_cells) > 0 else 0
            length_score = max(0, 1 - (avg_length / 10))

            # 总得分
            total_score = (text_ratio * 0.5) + (field_ratio * 0.3) + (length_score * 0.2)

            if total_score > best_score:
                best_score = total_score
                header_idx = idx
                headers = valid_cells

        # 兜底规则：如果评分未找到，取字段数最多的行
        if header_idx == -1 and len(all_data) > 0:
            max_fields = 0
            for idx, row in enumerate(all_data[:max_header_row]):
                if self._is_title_row(row):
                    continue
                valid_cells = [cell.strip() for cell in row if cell.strip()]
                if len(valid_cells) > max_fields:
                    max_fields = len(valid_cells)
                    header_idx = idx
                    headers = valid_cells

        return header_idx, headers

    # ========================================================
    # 字段名清洗与建表逻辑
    # ========================================================

    def _clean_field_name(self, field: str, fallback_idx: int) -> str:
        """
        清洗单个字段名，使其符合 SQLite 标识符规范。

        规则：
        - 保留中文、字母、数字、下划线
        - 其他字符替换为下划线
        - 首尾下划线去除
        - 如果结果为空，使用 fallback（col_N）

        Args:
            field:         原始字段名
            fallback_idx:  空字段名的兜底序号
        """
        clean = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9_]", "_", field.strip()) # 这是正则替换，把表头字段清洗成安全的数据库字段名（变量名）：不是中文、不是英文字母、不是数字、不是下划线的所有字符
        # field.strip() 是去掉首尾空格，避免字段名前后有空格导致数据库建表失败
        # re.sub(pattern, repl, string) re.sub (正则模式，替换成什么，待处理字符串)，把所有匹配正则的字符，替换为指定字符。
        """
        正则片段	     含义解释	
        r""	            原始字符串前缀（raw string）：告诉 Python “不要转义里面的字符”；	
        []	            字符集：匹配 “方括号内任意一个字符”；	
        ^	            在字符集[]开头：表示 “非”（取反）；	
        u4e00-u9fa5	中文汉字的 Unicode 编码范围：匹配所有简体中文汉字,覆盖从 “一”到 “龥”的所有汉字；
        a-zA-Z	        匹配所有大小写英文字母；	
        0-9	            匹配所有阿拉伯数字；	
        _	            匹配下划线,数据库字段名允许用下划线，所以保留；
        整体逻辑	     [^中文+字母+数字+下划线] → 匹配 “不是这四类的任意字符”；
        """
        clean = clean.strip("_") # 去掉字符串开头和结尾的下划线`_`，中间的下划线保留不动
        return clean if clean else f"col_{fallback_idx}"

    def _create_table(self, table_name: str, columns: List[str]) -> bool:
        """
        在数据库中创建业务表。

        Args:
            table_name: 表名
            columns:    清洗后的字段名列表

        Returns:
            True 表示成功
        """
        conn = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            col_defs = ", ".join([f"`{col}` TEXT" for col in columns]) # 专门用于SQLite 建表语句拼接，col_defs = ", ".join([f"`{col}` TEXT" for col in columns])的作用是将字符串列表的所有元素拼接成一个以逗号和空格分隔的单个字符串，最终得到类似 "`姓名` TEXT, `订单_金额` TEXT, `备注` TEXT" 的字符串，col_defs是一个新字符串
            # 先删旧表再建新表，防止重复导入导致数据堆积
            cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
            cursor.execute(f"CREATE TABLE IF NOT EXISTS `{table_name}` ({col_defs})")
            '''
            假设columns = ["姓名", "订单_金额", "备注"]
            1. 列表推导：[f"`{col}` TEXT" for col in columns]
            循环每一列名字，生成每一段 SQL 字段定义：
            "`姓名` TEXT"
            "`订单_金额` TEXT"
            "`备注` TEXT"
            SQLite 里`列名`反引号，用来包裹字段名，防止列名有中文、关键字导致语法报错。
            TEXT：代表该字段类型为文本，全部存字符串。

            2.", ".join(...)
            把列表里的每一段，用, （逗号 + 空格）拼接成一整条字符串：
            `姓名` TEXT, `订单_金额` TEXT, `备注` TEXT

            完整拼接成建表 SQL 示例
            sql = f"CREATE TABLE IF NOT EXISTS table_name ({col_defs});"
            最终得到 SQL：
            CREATE TABLE IF NOT EXISTS table_name (`姓名` TEXT, `订单_金额` TEXT, `备注` TEXT);
            这个 SQL 就可以直接在 SQLite 执行，创建一个表名为 table
            '''
            conn.commit()
            '''
            conn.commit()的作用：把内存里的事务修改，真正持久写到磁盘的 db 文件
            INSERT / UPDATE / DELETE / CREATE TABLE：修改数据，事务要提交，必须 commit，否则改动只在内存，关闭连接就直接丢失。
            SELECT：只读，不修改任何数据，没有东西要提交，完全不需要 commit。
            ''' 
            
            return True
        except sqlite3.Error as e: 
            print(f"[建表失败] {table_name}: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def _insert_data(self, table_name: str, columns: List[str], rows: List[List[str]]) -> int:
        """
        批量插入数据到业务表。

        Args:
            table_name: 表名
            columns:    字段名列表
            rows:       数据行列表

        Returns:
            成功插入的行数
        """
        conn = None
        try:
            conn = self._connect() # 连接数据库
            cursor = conn.cursor()
            col_list = ", ".join([f"`{col}`" for col in columns]) # 同上", ".join()返回的是一个新字符串，col_list = ", ".join([f"`{col}`" for col in columns])的作用是将字符串列表的所有元素拼接成一个以逗号和空格分隔的单个字符串，最终得到类似 "`姓名`, `订单_金额`, `备注`" 的字符串，col_list是一个新字符串
            placeholders = ", ".join(["?" for _ in columns]) # 占位符列表，长度与字段数一致，最终得到类似 "?, ?, ?" 的字符串
            insert_sql = f"INSERT OR IGNORE INTO `{table_name}` ({col_list}) VALUES ({placeholders})"
            cursor.executemany(insert_sql, rows)
            '''
            executemany(sql, seq_of_parameters)
            1. 第一个参数：sql：预编译好的 INSERT SQL 语句（带 ? 占位符）
            2. 第二个参数：rows：序列，里面每一个元素是一行数据，是元组 / 列表*。
            作用：批量插入多行，不用循环一遍一遍调用execute()

            executemany 本身不做校验！不会自动对齐！靠你上层业务代码保证！
            placeholders = ", ".join(["?" for _ in columns])
            columns有 N 列 → 生成 N 个?。SQL 语句里有 N 个问号。
            传给executemany的rows，其中的每一条记录，元素个数必须等于问号数量（等于 len (columns)）。
            如果对不上会直接报错
            '''
            conn.commit()
            return cursor.rowcount # 成功被插入的行数
        except sqlite3.Error as e: # 比如某行字段个数不对（比如 3 个？，行只有 2 个单元格）executemany()直接抛出sqlite3.ProgrammingError，行列长度不匹配这种 “数据结构错误” 不会被 OR IGNORE 放过，会整体失败回滚
            print(f"[数据插入失败] {table_name}: {e}")
            if conn:
                conn.rollback() # 数据库回滚，撤销本次事务里所有还没有 commit 提交的修改，一个都不会存
            return 0
        finally:
            if conn:
                conn.close()

    # ========================================================
    # 数据行过滤逻辑（从原代码完整保留）
    # ========================================================

    def _filter_valid_rows(self, data_rows: List[List[str]], headers: List[str]) -> List[List[str]]:
        """
        过滤无效数据行，保留有效业务数据。

        过滤规则（与原代码一致）：
        1. 行长度对齐（匹配表头字段数）
        2. 过滤全空行
        3. 过滤前 3 列均为空的行
        4. 过滤重复表头行（相似度 >= 0.7）
        5. 保留至少有一个非空单元格的行

        Args:
            data_rows: 表头行之后的所有数据行
            headers:   表头字段列表

        Returns:
            过滤后的有效数据行列表
        """

        # 可配置通用参数（无业务相关性，所有表格都适用）
        EMPTY_CHECK_COLS = 3  # 空行检查的列数（可调整）
        HEADER_SIMILARITY_THRESHOLD = 0.7  # 重复表头相似度阈值

        # 预处理：提取表头的有效单元格（去空、去重），用于相似度对比
        header_valid_cells = [cell.strip() for cell in headers if cell.strip()]
        header_unique = list(set(header_valid_cells)) # 列表去重变集合，list()再将集合变列表
        header_len = len(header_unique) if header_unique else 0

        valid_rows = [] # 将处理好的业务数据放入这里
        target_length = len(headers)

        for row in data_rows:
            # 1. 行长度对齐
            if len(row) < target_length:
                row = row + [""] * (target_length - len(row)) # 这个逻辑不会有问题，因为openpyxl 读取 Excel 一行，中间空单元格会保留为 ""，只有行最末尾连续的空单元格会被裁剪丢弃
            elif len(row) > target_length:
                row = row[:target_length] # 核心：截断表头外的多余空字符串

            # 2. 过滤全空行
            if all(cell.strip() == "" for cell in row):
                continue

            # 3. 过滤前 N 列均为空的行
            if all(cell.strip() == "" for cell in row[:EMPTY_CHECK_COLS]):
                continue

            # 4. 过滤重复表头行
            row_valid_cells = [cell.strip() for cell in row if cell.strip()]
            row_unique = list(set(row_valid_cells))
            if header_len > 0 and len(row_unique) > 0:
                common_cells = len(set(row_unique) & set(header_unique)) # 交集（&）：取两个集合中都存在的元素（比如 A={1,2,3}，B={2,3,4}，A&B={2,3}）
                similarity = common_cells / header_len
                # 相似度≥阈值 → 判定为重复表头，跳过
                if similarity >= HEADER_SIMILARITY_THRESHOLD:
                    continue

            # 5. 保留至少有一个非空单元格的行
            if any(cell.strip() != "" for cell in row): #any(可迭代对象) → 检查可迭代对象（列表、生成器等）里的元素：只要有 任意一个元素为 True → 返回 True；只有 所有元素都为 False → 才返回 False。
                '''
                从逻辑推导来说：
                all(cell.strip() == "" for cell in row) → 全空行，执行 continue 跳过；
                能走到 any(...) 这一步的行，必然不是全空行 → 必然满足 any(cell.strip() != "" for cell in row) → 这行判断在逻辑上确实 “多余”。
                但代码里依然保留这一步，是工程实践层面的考量（不是纯逻辑问题）—— 核心是「鲁棒性、可读性、防御性编程」
                '''
                valid_rows.append(row)
        return valid_rows

    # ========================================================
    # 合并单元格处理（从原代码完整保留）
    # ========================================================

    def _read_sheet_data(self, ws) -> List[List[str]]:
        """
        读取工作表所有数据，处理合并单元格。

        合并单元格的特点：只有左上角单元格有值，其他被合并的单元格为空。
        此方法遍历每个单元格，如果它在合并范围内，取左上角单元格的值。

        Args:
            ws: openpyxl 工作表对象，ws（工作表）：是具体的某个 Sheet（比如 “学生信息”），只有拿到这个 “文件”，才能读取 / 修改里面的单元格、行、列数据

        Returns:
            二维列表，每个元素是一行的单元格值列表
        """
        merged_ranges = list(ws.merged_cells.ranges) # 合并单元格范围
        '''
        1. 先记住 Excel 坐标的核心规则
        Excel 里的单元格坐标是「列字母 + 行数字」的组合，比如：
        列：用字母 A、B、C... 表示（A = 第 1 列，B = 第 2 列，C = 第 3 列，D = 第 4 列，E = 第 5 列……）；
        行：用数字 1、2、3... 表示（1 = 第 1 行，5 = 第 5 行……）；
        范围坐标（比如 A1:C3）：格式是「左上角单元格：右下角单元格」，表示从左上角到右下角的矩形区域。
        2. 逐个拆解你问的两个例子
        例子 1：A1:C3 → 第 1-3 行，第 1-3 列合并
        我们把 A1:C3 拆成「A1」（左上角）和「C3」（右下角）：
        A1:C3	整个合并区域	列 A 到 C（第 1-3 列）、行 1 到 3（第 1-3 行）
        简单说：A1:C3 就是「第 1 列到第 3 列、第 1 行到第 3 行」围成的 9 个单元格（A1、B1、C1、A2、B2、C2、A3、B3、C3）被合并成了一个单元格，只有左上角的 A1 有值。
        
        ws：是你从工作簿中取出的工作表对象（比如 “学生信息” 这个 Sheet）；
        ws.merged_cells：是该工作表中所有合并单元格的集合对象（可以理解为 “合并单元格管理器”）；
        ws.merged_cells.ranges：是这个集合中的具体合并范围列表（核心），每个元素代表一个合并单元格的 “区域”。
        原代码中用 list(ws.merged_cells.ranges) 将其转成列表，是为了方便后续遍历（不转也能遍历，只是转成列表更直观）；
        简单说：ws.merged_cells.ranges 会返回工作表中所有合并单元格的区域对象，每个对象描述了 “哪几行哪几列被合并了”（比如 A1:C3 这个区域是合并单元格）。
        举个直观例子：如果你的 Sheet 里有两个合并单元格：
        A1:C3（第 1-3 行，第 1-3 列合并）
        D5:E5（第 5 行，第 4-5 列合并）
        那么 ws.merged_cells.ranges 会返回包含两个 “区域对象” 的列表，打印出来大概是：
        [<MergedCellRange A1:C3>, <MergedCellRange D5:E5>]

        Excel 中合并单元格的特点是：只有左上角的单元格有值，其他被合并的单元格本身没有值。比如 A1:C3 合并后，只有 A1 有值，B1、C1、A2 等单元格的值都是空的。
        原代码中 merged_ranges = list(ws.merged_cells.ranges) 的核心目的是：遍历每个单元格时，检查它是否在某个合并范围内，如果是，就取该合并区域左上角单元格的值（而不是用当前单元格的空值），保证数据不丢失。
        '''
        max_col = ws.max_column # 获取当前Sheet的最大列数（解决单元格存在性问题）
        all_data = []
        # 用enumerate获取当前行的实际行号（start=1，Excel行号从1开始）
        for row_num, _ in enumerate(ws.iter_rows(values_only=False), start=1):
        # 遍历工作表的每一行（返回的是“单元格对象组成的行”）
            '''
            1. 先搞懂：ws.iter_rows () 是什么？
            ws 是工作表对象（比如 “学生信息” Sheet），ws.iter_rows() 是该对象的行迭代方法，核心作用是：
            按行遍历工作表中的所有单元格，返回 “行→单元格” 的层级结构（先拿到一行，再遍历这一行的每个单元格）；
            可以通过参数控制返回的内容（是单元格对象，还是仅单元格的值）、遍历的行 / 列范围。
            简单类比：把 Excel Sheet 想象成一个表格，ws.iter_rows() 就像 “逐行扫描器”—— 先扫第一行，把这一行的所有单元格装成一个 “行容器” 返回；再扫第二行，以此类推，直到最后一行。
            2. 核心参数：values_only（原代码中是 False）
            这是 iter_rows() 最关键的参数，直接决定返回内容，也是你原代码中写 values_only=False 的原因：
            表格
            参数值	                    返回内容	           适用场景
            values_only=False（默认）	返回单元格对象（Cell）	需要获取单元格坐标、格式、合并状态等（原代码场景）
            values_only=True	       返回单元格的值（纯数据）	只需要读取数据，不需要单元格其他属性
            原代码中必须用 values_only=False，因为要通过 cell.coordinate（单元格坐标，比如 "A1"）检查是否在合并范围内 —— 如果用 True，只能拿到值，拿不到坐标，就没法处理合并单元格了。
            '''
            row_data = [] # 存储当前行处理后的所有单元格值
            # 遍历每一列（从1到max_col，强制覆盖所有列，不管单元格是否存在）
            '''
            Excel 界面中：列是「字母列标（A/B/C/D）」，行是「数字行号（1/2/3/4）」；
            openpyxl 代码中：列和行都用数字表示（行号 = Excel 行号，列号 = 字母列标对应的数字，A=1、B=2、C=3…）；
            你代码里的 ws.cell(row=行号, column=列号) 中：
            row：对应 Excel 的行号（数字，比如 2 = 第 2 行）；
            column：对应 Excel 列标的数字版（比如 3=C 列）
            '''
            for col_idx in range(1, max_col + 1):
                '''
                强制遍历从 1 到最大列数的所有列，不管单元格是否存在：
                存在的单元格：正常获取值；
                不存在的单元格：通过ws.cell(row=row_num, column=col_idx)获取到 Cell 对象，value 为 None，最终转成""；
                保证「列坐标与表头一一对应」，中间缺失的列填空，不会错位。
                '''
                cell = ws.cell(row=row_num, column=col_idx) # 通过「行号 + 列号」精准定位并获取 / 修改 Excel 中指定位置的单元格对象
                # 处理值（None 转 ""）
                cell_val = str(cell.value).strip() if cell.value is not None else ""# cell.value 就是获取 / 设置单元格具体值

                # 检查是否在合并范围内（不管单元格是不是合并范围的，都会走这个流程）
                for merged_range in merged_ranges: # merged_range 是 openpyxl 中的合并单元格区域对象（打印出来显示比如 <MergedCellRange A1:C3>）
                    if cell.coordinate in merged_range:
                        '''
                        cell 是 ws.iter_rows(values_only=False) 返回的单元格对象（比如 A1、B3 这些单元格对应的对象），而 cell.coordinate 是这个对象的一个只读属性，作用是：
                        返回该单元格的Excel 风格坐标字符串（比如 A1、B5、C10 这种格式）；
                        这个坐标和你在 Excel 界面上看到的单元格位置完全一致（列用字母，行用数字）。
                        '''
                        top_left_cell = ws[merged_range.coord.split(":")[0]] # 从一个合并单元格区域（比如 A1:C3）中，精准找到 “左上角那个有值的单元格”（比如 A1），因为 Excel 中合并单元格只有左上角单元格存储值，其他被合并的单元格值都是空的。
                        '''
                        merged_range.coord 就是这个对象的属性，专门返回该合并区域的Excel 风格坐标字符串（比如 "A1:C3"）
                        split(":")[0]      → 把坐标字符串切分，取第一个部分（如"A1"）左上角的位置
                        ws[xxx]            → 用坐标从工作表中取出对应的单元格对象
                        '''
                        cell_val = str(top_left_cell.value).strip() if top_left_cell.value else ""
                        break
                row_data.append(cell_val)
            all_data.append(row_data) # all_data 最终会收纳整个 Sheet 里每一个单元格的处理后值，而不管单元格是否属于合并范围、有没有自身值，都会走完 “取自身值 → 检查合并范围 → 确定最终值” 的完整流程。
            # all_data 是一个列表的列表（二维列表）

        return all_data

    # ========================================================
    # 主入口
    # ========================================================

    def import_excel(self, excel_path: str) -> Dict: # Excel 文件路径不是写死在代码里的，必须由用户输入给到大模型
        """
        导入 Excel 文件到数据库。

        流程：
        1. 加载 Excel 工作簿
        2. 遍历每个 Sheet：
           a. 读取数据（处理合并单元格）
           b. 识别表头行
           c. 过滤无效数据行
           d. 清洗字段名
           e. 创建业务表
           f. 插入数据
           g. 记录元数据（只有在import_excel中会在建立好的元数据表中插入数据）
        3. 返回结构化结果

        Args:
            excel_path: Excel 文件路径

        Returns: # 可以看成是一个小总结
            {
            "success": bool,                     # bool：布尔值 True/False，代表整体处理是否成功
            "tables": [                          # tables：列表，列表里面每一项是一个table字典对象
                {
                    "table_name": str,           # str字符串：生成的数据库表名
                    "original_sheet_name": str,  # str字符串：Excel原始sheet名称
                    "columns": [str, ...],       # [str,...]：字符串列表，代表该表的列名集合
                    "row_count": int             # int整数：该表有效数据行数
                },
                ...                              # ...代表列表里还可以有更多个这种table对象
            ],
            "skipped_sheets": [str, ...],        # [str,...]：字符串列表，被跳过、没有处理的sheet名字集合
            "error": str | None                  # str|None：出错信息，成功的时候为None，失败存错误文本
            }
        """
        result = {
            "success": False,
            "tables": [],
            "skipped_sheets": [],
            "error": None,
        } # 返回值

        #excel_path：传入的 Excel 文件路径（字符串类型，如"D:/竞赛数据.xlsx"）
        if not os.path.exists(excel_path):
            result["error"] = f"Excel文件不存在: {excel_path}"
            return result

        # 加载 Excel
        try:
            wb = load_workbook(excel_path, data_only=False) # load_workbook()从指定路径读取 Excel 文件（.xlsx 格式），返回一个 “工作簿对象”（可以理解为 “内存中的 Excel 文件”）；
        except Exception as e:
            result["error"] = f"加载Excel失败: {e}"
            return result

        # 遍历每个 Sheet
        for sheet_name in wb.sheetnames:
            # wb.sheetnames 是这个对象的一个只读属性，作用是：返回一个列表，列表中的每个元素是 Excel 文件里所有 Sheet 的名称字符串。顺序和你在 Excel 里看到的 Sheet 标签顺序完全一致。
            ws = wb[sheet_name]
            '''
            先理清核心关系：工作簿 vs 工作表
            你可以把 Excel 文件想象成一个文件夹（工作簿 wb），里面装着多个文件（工作表 ws）：
            wb（工作簿）：是整个 Excel 文件的 “容器”，包含所有 Sheet，但它本身不能直接操作某一行 / 某一列数据；
            ws（工作表）：是具体的某个 Sheet（比如 “学生信息”），只有拿到这个 “文件”，才能读取 / 修改里面的单元格、行、列数据。
            为什么需要 wb[sheet_name]？
            for sheet_name in wb.sheetnames 只是把Sheet 名称字符串（比如 "学生信息"）赋值给 sheet_name，但这个字符串本身没有任何数据操作能力 —— 你需要用这个 “名称” 作为 “钥匙”，从工作簿 wb 中取出对应的工作表对象（ws），才能操作这个 Sheet 里的数据。
            '''
            print(f"\n----------------处理Sheet：{sheet_name}----------------")

            # 1. 读取数据（处理合并单元格）
            # all_data 是一个二维列表，里面每个元素都是一行的单元格值列表（每个单元格值都是字符串类型，空单元格是 ""）
            all_data = self._read_sheet_data(ws) # all_data 最终会收纳整个 Sheet 里每一个单元格的处理后值，而不管单元格是否属于合并范围、有没有自身值，都会走完 “取自身值 → 检查合并范围 → 确定最终值” 的完整流程。

            if len(all_data) == 0:
                result["skipped_sheets"].append(f"{sheet_name}（空Sheet）")
                continue

            # 2. 识别表头
            header_idx, headers = self._find_header_row(all_data, max_header_row=10) # header_idx, headers分别为表头行索引和有效表头字段列表

            if header_idx == -1 or len(headers) == 0:
                result["skipped_sheets"].append(f"{sheet_name}（未找到表头）")
                continue

            # 3. 过滤无效数据行
            data_rows = all_data[header_idx + 1:] # 表头行之后的所有数据行
            valid_rows = self._filter_valid_rows(data_rows, headers)

            if len(valid_rows) == 0:
                result["skipped_sheets"].append(f"{sheet_name}（无有效数据）")
                continue

            # 4. 清洗字段名
            columns = [
                self._clean_field_name(h, idx + 1) for idx, h in enumerate(headers)
            ]

            # 5. 生成表名（清洗 Sheet 名，不加 table_type 前缀）
            table_name = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9_]", "_", sheet_name).strip("_")
            if not table_name:
                table_name = f"sheet_{len(result['tables']) + 1}"

            # 6. 创建业务表
            if not self._create_table(table_name, columns):
                result["skipped_sheets"].append(f"{sheet_name}（建表失败）")
                continue

            # 7. 插入数据
            inserted = self._insert_data(table_name, columns, valid_rows) # inserted为成功插入的行数

            # 8. 记录元数据
            self.metadata_manager.record_table(
                table_name=table_name,
                original_sheet_name=sheet_name,
                columns=columns,
                row_count=inserted,
                source_file=excel_path,
            )

            result["tables"].append(
                {
                    "table_name": table_name,
                    "original_sheet_name": sheet_name,
                    "columns": columns,
                    "row_count": inserted,
                }
            )

        wb.close()

        result["success"] = len(result["tables"]) > 0 # 如果至少有一个表成功导入，success 就是 True，否则就是 False
        if not result["success"]:
            result["error"] = "所有Sheet均导入失败或无有效数据"

        return result
