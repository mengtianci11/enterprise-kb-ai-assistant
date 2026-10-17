"""生成用于演示与验证的多格式样本文件。

生成内容：
- samples/员工手册.docx      ：标题层级 + 表格（验证结构解析与表格语义化）
- samples/库存台账.xlsx      ：多 sheet 数据表（验证 Excel 解析）
- samples/产品介绍.pptx      ：标题 + 备注页（验证备注提取）
- samples/扫描件示例.pdf     ：文本渲染成图后再封装为 PDF（无文本层，验证 OCR 链路）

用法：python scripts/make_samples.py
"""
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
SAMPLES.mkdir(exist_ok=True)


def make_docx() -> pathlib.Path:
    from docx import Document

    doc = Document()
    doc.add_heading("员工手册（2025 版）", level=1)
    doc.add_heading("第三章 休假制度", level=2)
    doc.add_paragraph("员工入职满一年后，每年享有5天带薪年假；工作满三年后，年假增加至10天。")
    doc.add_paragraph("年假需提前3个工作日通过OA系统申请，经直属主管批准后方可休假。")
    doc.add_heading("3.1 差旅住宿标准", level=3)
    table = doc.add_table(rows=3, cols=3)
    headers = ["城市级别", "住宿标准", "备注"]
    for i, h in enumerate(headers):
        table.cell(0, i).text = h
    rows = [
        ["一线城市", "500 元/晚", "含税"],
        ["其他城市", "350 元/晚", "超标自理"],
    ]
    for r, row in enumerate(rows, start=1):
        for c, val in enumerate(row):
            table.cell(r, c).text = val

    path = SAMPLES / "员工手册.docx"
    doc.save(path)
    return path


def make_xlsx() -> pathlib.Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "库存台账"
    ws.append(["产品编码", "产品名称", "库存数量", "单价(元)"])
    ws.append(["P001", "工业传感器A", 120, 350])
    ws.append(["P002", "工业传感器B", 80, 420])
    ws.append(["P003", "数据采集网关", 45, 1280])

    ws2 = wb.create_sheet("供应商")
    ws2.append(["供应商", "联系人", "账期"])
    ws2.append(["华信电子", "张经理", "30天"])
    ws2.append(["东方仪器", "李经理", "45天"])

    path = SAMPLES / "库存台账.xlsx"
    wb.save(path)
    return path


def make_pptx() -> pathlib.Path:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "第三季度经营总结"
    notes = slide.notes_slide.notes_text_frame
    notes.text = "关键结论：营收环比增长20%，主要来自华东区大客户续约。"
    path = SAMPLES / "产品介绍.pptx"
    prs.save(path)
    return path


def make_scanned_pdf() -> pathlib.Path:
    """先生成带文字的页面，再整页栅格化为图片 PDF（模拟扫描件，无文本层）。"""
    import pymupdf

    src = pymupdf.open()
    page = src.new_page(width=595, height=300)  # A4 宽度的半页
    page.insert_text(
        (50, 80),
        "公司保密制度（扫描件）",
        fontname="china-s",
        fontsize=22,
    )
    page.insert_text(
        (50, 140),
        "员工对在职期间接触的商业机密负有保密义务，离职后保密义务继续有效。",
        fontname="china-s",
        fontsize=14,
    )
    page.insert_text(
        (50, 180),
        "禁止将公司文档外发至未授权渠道。",
        fontname="china-s",
        fontsize=14,
    )
    pix = page.get_pixmap(dpi=200)
    img_bytes = pix.tobytes("png")
    src.close()

    out = pymupdf.open()
    out_page = out.new_page(width=595, height=300)
    out_page.insert_image(pymupdf.Rect(0, 0, 595, 300), stream=img_bytes)
    path = SAMPLES / "扫描件示例.pdf"
    out.save(path)
    out.close()
    return path


if __name__ == "__main__":
    made = []
    for fn in (make_docx, make_xlsx, make_pptx, make_scanned_pdf):
        try:
            made.append(fn())
        except Exception as exc:  # noqa: BLE001
            print(f"[失败] {fn.__name__}: {exc}", file=sys.stderr)
    for p in made:
        print(f"已生成 {p.relative_to(ROOT)}  ({p.stat().st_size} 字节)")
