#!/usr/bin/env python3
"""
自动更新构建脚本 — 替换源文件后运行即可刷新所有HTML
用法: python3 build.py
"""
import json, os, re, shutil, subprocess, tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(BASE)

# ============================================================
# 1. 解析 script.sql → DATA + DDL
# ============================================================
def parse_layer(name):
    """根据表名前缀推断数仓分层"""
    n = name.lower()
    if n.startswith('v_dim') or n.startswith('dim_'):   return 'DIM'
    if n.startswith('v_dwd')  or n.startswith('dwd_'):  return 'DWD'
    if n.startswith('v_dws')  or n.startswith('dws_'):  return 'DWS'
    if n.startswith('v_ods')  or n.startswith('ods_'):  return 'ODS'
    if n.startswith('v_ads')  or n.startswith('ads_'):  return 'ADS'
    if n.startswith('v_rpt')  or n.startswith('rpt_'):  return 'ADS'
    if n.startswith('dict_')  or n.startswith('crm_'):  return 'OTHER'
    return 'OTHER'

def parse_sql_file(path):
    """解析CREATE TABLE/VIEW语句，返回DATA字典"""
    with open(path, 'r', encoding='utf-8') as f:
        sql = f.read()

    data = {}
    # 按CREATE TABLE/VIEW拆分
    blocks = re.split(r'(?i)(?=create\s+(?:table|view)\s+)', sql)
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # CREATE VIEW
        view_m = re.match(r'create\s+view\s+[\w.]*?(\w+)\s*\(', block, re.I)
        if view_m:
            name = view_m.group(1)
            # 提取字段
            fields = []
            for fm in re.finditer(r'`?(\w+)`?\s+(\S+(?:\([^)]*\))?)', block):
                fname, ftype = fm.group(1), fm.group(2)
                if fname.lower() in ('select','from','where','as','on','and','or','left','join','inner','outer','union','group','by','order','having','limit','null','not','in','is','like','between','exists','case','when','then','else','end','distinct','if'):
                    continue
                fields.append({'name': fname, 'type': ftype, 'comment': ''})
            data[name] = {
                'layer': parse_layer(name),
                'fields': fields,
                'lineage': {'workflow': '', 'sources': []},
                'cn_name': '',
                'is_view': True,
                'ddl': block.rstrip(';').strip()
            }
            continue

        # CREATE TABLE
        tbl_m = re.match(r'create\s+table\s+(\w+)', block, re.I)
        if not tbl_m:
            continue
        name = tbl_m.group(1)

        # 找到字段定义区域 (第一个( 到匹配的))
        paren_start = block.index('(')
        depth, paren_end = 0, -1
        for i, c in enumerate(block[paren_start:], paren_start):
            if c == '(': depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    paren_end = i
                    break
        if paren_end < 0:
            continue

        body = block[paren_start+1:paren_end]
        rest = block[paren_end+1:]

        # 提取字段: name Type comment 'xxx'
        fields = []
        for fm in re.finditer(
            r'(\w+)\s+'
            r'((?:Nullable\()?(?:String|UInt\d+|Int\d+|Float\d+|Decimal\(\d+,\s*\d+\)|Date|DateTime|FixedString\(\d+\)|Array\([^)]+\)|Map\([^)]+\)|UUID|Boolean|LowCardinality\([^)]+\))(?:\))?(?:\([^)]*\))?)'
            r"(?:\s+comment\s+'([^']*)')?",
            body, re.I
        ):
            fields.append({
                'name': fm.group(1),
                'type': fm.group(2),
                'comment': fm.group(3) or ''
            })

        # 提取表注释
        cn_m = re.search(r"COMMENT\s+'([^']*)'", rest)
        cn_name = cn_m.group(1) if cn_m else ''

        data[name] = {
            'layer': parse_layer(name),
            'fields': fields,
            'lineage': {'workflow': '', 'sources': []},
            'cn_name': cn_name,
            'is_view': False,
            'ddl': block.rstrip(';').strip()
        }

    return data

# ============================================================
# 2. 解析 workflow JSON → WORKFLOWS
# ============================================================
def extract_sources_from_sql(sql_text):
    """从SQL中提取FROM/JOIN后面的表名作为数据源"""
    if not sql_text:
        return []
    # 移除注释
    clean = re.sub(r'--[^\n]*', '', sql_text)
    clean = re.sub(r'/\*.*?\*/', '', clean, flags=re.S)
    # 提取 FROM/JOIN 后的表名
    tables = set()
    for m in re.finditer(r'(?:FROM|JOIN)\s+(\w+)', clean, re.I):
        t = m.group(1).lower()
        if t not in ('select','where','and','or','on','as','left','right','inner','outer','union','all','set','null','not','in','dual','d','a','b','c','e','f','g','s','t','u','v','w','x','tmp'):
            tables.add(t)
    return sorted(tables)

def parse_workflow_json(path):
    """解析海豚调度导出JSON，返回WORKFLOWS字典"""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    workflows = {}
    for wf in data:
        pd = wf['processDefinition']
        wf_name = pd['name']
        tasks_out = []

        # 构建code->name映射
        code_to_name = {}
        for t in wf.get('taskDefinitionList', []):
            code_to_name[t['code']] = t['name']

        for t in wf.get('taskDefinitionList', []):
            name = t['name']
            task_type = t['taskType']
            params = t.get('taskParams', {})

            # 提取SQL
            sql = params.get('sql', '') or params.get('rawScript', '') or ''
            if isinstance(sql, dict):
                sql = ''

            # 提取目标表
            target = params.get('targetTable', '') or ''

            # 提取数据源
            sources = extract_sources_from_sql(sql) if sql else []

            tasks_out.append({
                'name': name,
                'type': task_type,
                'target': target,
                'sql': sql,
                'sources': sources
            })

        workflows[wf_name] = tasks_out

    return workflows

# ============================================================
# 3. 解析 CHM → NC65页面 + NC_TABLE_MAP
# ============================================================
def parse_chm(chm_path):
    """解压CHM并解析，返回 (modules, table_to_html_map)
    modules: [{'name': '模块名', 'tables': [{'name','cn_name','html'}]}]
    table_to_html_map: {table_name: html_filename}
    """
    tmpdir = tempfile.mkdtemp()
    try:
        subprocess.run(['7z', 'x', chm_path, f'-o{tmpdir}', '-y'],
                       capture_output=True, check=True)

        # 解析TOC
        toc_path = os.path.join(tmpdir, '000_toc.hhc')
        with open(toc_path, 'rb') as f:
            toc_text = f.read().decode('gb2312', errors='ignore')

        modules = []
        current_module = None
        table_to_html = {}

        for m in re.finditer(r'<OBJECT[^>]*>(.*?)</OBJECT>', toc_text, re.S):
            block = m.group(1)
            name_m = re.search(r'Name.*?value="(.*?)"', block)
            local_m = re.search(r'Local.*?value="(.*?)"', block)
            imgnum_m = re.search(r'ImageNumber.*?value="(\d+)"', block)

            if not name_m:
                continue

            name = name_m.group(1)
            local = local_m.group(1) if local_m else None
            is_module = imgnum_m and imgnum_m.group(1) == '1' and local is None

            if is_module:
                current_module = name
                modules.append({'name': name, 'tables': []})
            elif local and current_module:
                parts = name.split(' ', 1)
                table_name = parts[0]
                cn_name = parts[1] if len(parts) > 1 else ''
                modules[-1]['tables'].append({
                    'name': table_name,
                    'cn_name': cn_name,
                    'html': local
                })
                table_to_html[table_name] = local

        return modules, table_to_html
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ============================================================
# 4. 注入常量到CK HTML
# ============================================================
def inject_ck_constants(html_path, data, workflows, nc_table_map):
    """替换CK HTML中的DATA/WORKFLOWS/NC_TABLE_MAP常量"""
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()

    # DATA
    data_json = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    html = re.sub(
        r'const DATA = \{[\s\S]*?\};\n',
        lambda m: f'const DATA = {data_json};\n',
        html
    )

    # WORKFLOWS
    wf_json = json.dumps(workflows, ensure_ascii=False, separators=(',', ':'))
    html = re.sub(
        r'const WORKFLOWS = \{[\s\S]*?\};\n',
        lambda m: f'const WORKFLOWS = {wf_json};\n',
        html
    )

    # NC_TABLE_MAP
    map_json = json.dumps(nc_table_map, ensure_ascii=False, separators=(',', ':'))
    html = re.sub(
        r'const NC_TABLE_MAP = \{[\s\S]*?\};\n',
        lambda m: f'const NC_TABLE_MAP = {map_json};\n',
        html
    )

    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)

# ============================================================
# 5. 生成NC65 HTML
# ============================================================
def generate_nc65_html(nc_path, modules, table_to_html):
    """重新生成NC65 HTML的数据部分（保留头部样式和脚本）"""
    with open(nc_path, 'r', encoding='utf-8') as f:
        html = f.read()

    # 找到 <ul class="module-list" 开始位置
    ul_start = html.index('<ul class="module-list"')
    # 找到 </ul> 结束
    ul_end = html.index('</ul>', ul_start) + len('</ul>')

    # 找到 <section class="module-section" 开始位置（第一个）
    sec_start = html.index('<section class="module-section"')
    # 找到最后一个 </section> 后面的 </main>
    main_end = html.index('</main>', sec_start)

    total_tables = sum(len(m['tables']) for m in modules)

    # 生成侧边栏
    sidebar_items = []
    for idx, mod in enumerate(modules):
        tcount = len(mod['tables'])
        sidebar_items.append(
            f'<li><a href="#module-{idx}">'
            f'<span>{mod["name"]} <span class="table-count">({tcount})</span></span>'
            f'</a></li>'
        )
    sidebar_html = '\n'.join(sidebar_items)

    # 生成内容区
    sections = []
    for idx, mod in enumerate(modules):
        items = []
        for t in mod['tables']:
            href = f'pages/{t["html"]}'
            desc = t['cn_name'] or t['name']
            items.append(
                f'                <div class="table-item">\n'
                f'                    <a href="{href}" target="_blank">\n'
                f'                        <div class="table-name">{t["name"]}</div>\n'
                f'                        <div class="table-desc">{desc}</div>\n'
                f'                    </a>\n'
                f'                </div>'
            )
        sections.append(
            f'        <section class="module-section" id="module-{idx}">\n'
            f'            <h2>{mod["name"]}</h2>\n'
            f'            <div class="table-list">\n'
            + '\n\n'.join(items) + '\n'
            f'            </div>\n'
            f'        </section>'
        )
    sections_html = '\n        \n'.join(sections)

    # 更新统计数字
    html = re.sub(
        r'模块: <strong>\d+</strong>',
        f'模块: <strong>{len(modules)}</strong>',
        html
    )
    html = re.sub(
        r'表: <strong>\d+</strong>',
        f'表: <strong>{total_tables}</strong>',
        html
    )

    # 替换侧边栏
    html = html[:ul_start] + f'<ul class="module-list" id="moduleList">\n{sidebar_html}\n' + html[ul_end:]

    # 重新计算section开始位置（因为前面的替换可能改变了偏移）
    sec_start = html.index('<section class="module-section"')
    main_end = html.index('</main>', sec_start)

    html = html[:sec_start] + sections_html + '\n        \n    ' + html[main_end:]

    with open(nc_path, 'w', encoding='utf-8') as f:
        f.write(html)

# ============================================================
# 6. 建立WORKFLOWS → DATA的lineage映射
# ============================================================
def build_lineage(data, workflows):
    """根据WORKFLOWS任务信息，为DATA中每个表建立lineage"""
    for wf_name, tasks in workflows.items():
        for task in tasks:
            target = task.get('target', '')
            if target and target in data:
                data[target]['lineage']['workflow'] = wf_name
                data[target]['lineage']['sources'] = task.get('sources', [])

# ============================================================
# 7. 建立NC_TABLE_MAP
# ============================================================
def build_nc_table_map(data, table_to_html):
    """从DATA中的sources找出NC表名，映射到CHM HTML文件"""
    nc_map = {}
    for name, info in data.items():
        for src in info.get('lineage', {}).get('sources', []):
            if src in table_to_html:
                nc_map[src] = table_to_html[src]
    return nc_map

# ============================================================
# MAIN
# ============================================================
def main():
    src_dir = BASE
    ck_dir = os.path.join(PROJECT, 'ClickHouse')
    nc_dir = os.path.join(PROJECT, 'NC65')

    # 查找源文件
    sql_file = os.path.join(src_dir, 'script.sql')
    wf_file = None
    chm_file = os.path.join(src_dir, '65数据字典.chm')

    for f in os.listdir(src_dir):
        if f.startswith('workflow_') and f.endswith('.json'):
            wf_file = os.path.join(src_dir, f)
            break

    # 检查文件存在
    missing = []
    if not os.path.exists(sql_file): missing.append('script.sql')
    if not wf_file:                 missing.append('workflow_*.json')
    if not os.path.exists(chm_file): missing.append('65数据字典.chm')
    if missing:
        print(f'[ERROR] 缺少源文件: {", ".join(missing)}')
        return

    print('[1/5] 解析 script.sql ...')
    data = parse_sql_file(sql_file)
    print(f'      -> {len(data)} 张表/视图')

    print('[2/5] 解析 workflow JSON ...')
    workflows = parse_workflow_json(wf_file)
    print(f'      -> {len(workflows)} 个工作流')

    print('[3/5] 解析 65数据字典.chm ...')
    modules, table_to_html = parse_chm(chm_file)
    total_nc = sum(len(m['tables']) for m in modules)
    print(f'      -> {len(modules)} 个模块, {total_nc} 张表')

    print('[4/5] 建立映射关系 ...')
    build_lineage(data, workflows)
    nc_table_map = build_nc_table_map(data, table_to_html)
    print(f'      -> NC_TABLE_MAP: {len(nc_table_map)} 条')

    print('[5/5] 更新HTML文件 ...')
    ck_html = os.path.join(ck_dir, '数据字典查询.html')
    nc_html = os.path.join(nc_dir, 'nc字典查询.html')

    if os.path.exists(ck_html):
        inject_ck_constants(ck_html, data, workflows, nc_table_map)
        print(f'      -> CK: {ck_html}')
    else:
        print(f'      [WARN] CK文件不存在: {ck_html}')

    if os.path.exists(nc_html):
        generate_nc65_html(nc_html, modules, table_to_html)
        print(f'      -> NC65: {nc_html}')
    else:
        print(f'      [WARN] NC65文件不存在: {nc_html}')

    print('\n完成! 所有HTML已更新。')

if __name__ == '__main__':
    main()
