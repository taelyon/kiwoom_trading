"""
PDF 파일을 텍스트로 변환하는 스크립트
키움 REST API 문서를 읽기 위해 사용
"""

import PyPDF2
import pdfplumber
import fitz  # PyMuPDF
import sys
import os

def read_pdf_with_pypdf2(pdf_path):
    """PyPDF2를 사용하여 PDF 읽기"""
    try:
        with open(pdf_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            text = ""
            for page_num in range(len(pdf_reader.pages)):
                page = pdf_reader.pages[page_num]
                text += f"\n--- 페이지 {page_num + 1} ---\n"
                text += page.extract_text()
            return text
    except Exception as e:
        print(f"PyPDF2로 PDF 읽기 실패: {e}")
        return None

def read_pdf_with_pdfplumber(pdf_path):
    """pdfplumber를 사용하여 PDF 읽기"""
    try:
        text = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                text += f"\n--- 페이지 {page_num + 1} ---\n"
                page_text = page.extract_text()
                if page_text:
                    text += page_text
        return text
    except Exception as e:
        print(f"pdfplumber로 PDF 읽기 실패: {e}")
        return None

def read_pdf_with_pymupdf(pdf_path):
    """PyMuPDF를 사용하여 PDF 읽기"""
    try:
        doc = fitz.open(pdf_path)
        text = ""
        for page_num in range(doc.page_count):
            page = doc[page_num]
            text += f"\n--- 페이지 {page_num + 1} ---\n"
            text += page.get_text()
        doc.close()
        return text
    except Exception as e:
        print(f"PyMuPDF로 PDF 읽기 실패: {e}")
        return None

def search_in_pdf(pdf_path, search_terms):
    """PDF에서 특정 용어 검색"""
    results = {}
    
    # PyMuPDF로 검색 (가장 정확함)
    try:
        doc = fitz.open(pdf_path)
        for term in search_terms:
            results[term] = []
            for page_num in range(doc.page_count):
                page = doc[page_num]
                text_instances = page.search_for(term)
                if text_instances:
                    results[term].append({
                        'page': page_num + 1,
                        'instances': len(text_instances),
                        'context': page.get_text()
                    })
        doc.close()
    except Exception as e:
        print(f"PDF 검색 실패: {e}")
    
    return results

def main():
    pdf_path = "키움 REST API 문서.pdf"
    
    if not os.path.exists(pdf_path):
        print(f"PDF 파일을 찾을 수 없습니다: {pdf_path}")
        return
    
    print("PDF 파일 읽기 시도 중...")
    
    # 여러 방법으로 PDF 읽기 시도
    text = None
    
    # 1. PyMuPDF 시도 (가장 좋음)
    text = read_pdf_with_pymupdf(pdf_path)
    
    # 2. pdfplumber 시도
    if not text:
        text = read_pdf_with_pdfplumber(pdf_path)
    
    # 3. PyPDF2 시도
    if not text:
        text = read_pdf_with_pypdf2(pdf_path)
    
    if text:
        print("PDF 읽기 성공!")
        
        # 웹소켓 관련 내용 검색
        search_terms = ["websocket", "WebSocket", "웹소켓", "실시간", "REAL", "trnm", "구독"]
        results = search_in_pdf(pdf_path, search_terms)
        
        print("\n=== 검색 결과 ===")
        for term, matches in results.items():
            if matches:
                print(f"\n'{term}' 검색 결과:")
                for match in matches:
                    print(f"  - 페이지 {match['page']}: {match['instances']}개 발견")
        
        # 텍스트 파일로 저장
        output_file = "키움_REST_API_문서_텍스트.txt"
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f"\n텍스트가 '{output_file}'에 저장되었습니다.")
        
        # 웹소켓 관련 부분만 추출
        websocket_sections = []
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if any(term.lower() in line.lower() for term in ["websocket", "웹소켓", "실시간", "real"]):
                # 해당 라인 주변 10줄씩 추출
                start = max(0, i - 10)
                end = min(len(lines), i + 10)
                section = '\n'.join(lines[start:end])
                websocket_sections.append(section)
        
        if websocket_sections:
            with open("웹소켓_관련_내용.txt", 'w', encoding='utf-8') as f:
                f.write('\n\n'.join(websocket_sections))
            print("웹소켓 관련 내용이 '웹소켓_관련_내용.txt'에 저장되었습니다.")
        
    else:
        print("PDF 읽기에 실패했습니다. 필요한 라이브러리를 설치해주세요:")
        print("pip install PyPDF2 pdfplumber PyMuPDF")

if __name__ == "__main__":
    main()
