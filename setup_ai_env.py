import os
import sys

# 프로젝트 루트 경로를 sys.path에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config_manager import EnvConfigParser

def setup_ai_strategies():
    print("AI 기반 전략 설정을 .env에 반영합니다...")
    
    # 설정 관리자 인스턴스 생성
    config = EnvConfigParser()
    
    # 통합 전략(Integrated)에 AI 매수 전략 세팅 (이미 있으면 덮어씀)
    # AI_SCORE > 0.75
    config.set('INTEGRATED', 'buy_stg_0', '{"name": "AI 정밀 매수", "content": "AI_SCORE > 0.75"}')
    config.set('INTEGRATED', 'buy_stg_1', '{"name": "기본 눌림목 매수", "content": "tic_RSI[-1] < 30 and tic_MACD_HIST[-1] > 0"}')
    
    # 통합 전략에 AI 매도 전략 세팅
    # AI_SCORE < 0.3 and current_profit_pct < -1.0
    config.set('INTEGRATED', 'sell_stg_0', '{"name": "AI 조기 매도", "content": "AI_SCORE < 0.3 and current_profit_pct < -1.0", "partial_sell_ratio": 1.0}')
    config.set('INTEGRATED', 'sell_stg_1', '{"name": "수익 보존(익절)", "content": "current_profit_pct > 3.0", "partial_sell_ratio": 1.0}')
    config.set('INTEGRATED', 'sell_stg_2', '{"name": "기계적 손절", "content": "current_profit_pct < -2.0", "partial_sell_ratio": 1.0}')
    
    # STRATEGIES 섹션에 통합 전략이 등록되어 있는지 확인하고 없으면 등록
    has_integrated = False
    if config.has_section('STRATEGIES'):
        for k, v in config.items('STRATEGIES'):
            if v == 'INTEGRATED':
                has_integrated = True
                break
                
    if not has_integrated:
        config.set('STRATEGIES', 'stg_integrated', 'INTEGRATED')
        print("STRATEGIES 목록에 'INTEGRATED' 전략을 등록했습니다.")
        
    # 변경사항 저장
    config.save()
    print(".env 파일에 AI 매수/매도 전략이 성공적으로 반영되었습니다.")
    
    # 저장된 내용 확인
    print("\n--- [저장된 통합 매수 전략] ---")
    items = sorted([item for item in config.items('INTEGRATED') if item[0].startswith('buy_stg_')], key=lambda x: int(x[0].split('_')[-1]))
    for k, v in items:
        print(f"{k}: {v}")
        
    print("\n--- [저장된 통합 매도 전략] ---")
    items = sorted([item for item in config.items('INTEGRATED') if item[0].startswith('sell_stg_')], key=lambda x: int(x[0].split('_')[-1]))
    for k, v in items:
        print(f"{k}: {v}")

if __name__ == "__main__":
    setup_ai_strategies()
