import os
import io
import logging
from dotenv import load_dotenv, set_key, find_dotenv

# 프로젝트에서 관리하는 .env 키 접두사 목록
_MANAGED_PREFIXES = (
    'STRATEGIES_', 'BUYCOUNT_', 'TRADING_', 'DATA_SAVING_', 'CHART_',
    'KIWOOM_API_', 'LOGIN_', 'SLACK_', 'SETTINGS_', 'STRATEGY_', 'API_',
)

# 표준 섹션 이름 (대문자)
_STANDARD_SECTIONS = frozenset([
    'STRATEGIES', 'BUYCOUNT', 'TRADING', 'DATA_SAVING', 'CHART',
    'KIWOOM_API', 'LOGIN', 'SLACK', 'SETTINGS', 'API',
])


class EnvConfigParser:
    """ConfigParser와 호환되는 .env 기반 설정 관리자 (Shim 클래스)"""
    
    # 싱글톤 인스턴스
    _instance = None
    
    def __new__(cls):
        """싱글톤 패턴: 항상 동일한 인스턴스를 반환하여 .env 파싱 I/O를 최소화"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        # 이미 초기화된 경우 건너뛰기
        if self._initialized:
            return
        self._initialized = True
        
        self.logger = logging.getLogger(self.__class__.__name__)
        self.env_path = find_dotenv() or '.env'
        if not os.path.exists(self.env_path):
            with open(self.env_path, 'w', encoding='utf-8') as f:
                f.write("# Generated .env file\n")
        
        load_dotenv(self.env_path, override=True, encoding='utf-8')
        self._data = {}
        self._sync_from_env()
        self._ensure_integrated_strategy()

    def _sync_from_env(self):
        """환경 변수 및 파일에서 캐시로 데이터 동기화 (관리 대상 키만)"""
        from dotenv import dotenv_values
        file_env = dotenv_values(self.env_path, encoding='utf-8')
        
        # 1. 파일에서 읽은 값을 우선적으로 딕셔너리에 저장 (한글 키 깨짐 방지)
        for k, v in file_env.items():
            if v is not None and any(k.startswith(p) for p in _MANAGED_PREFIXES):
                self._data[k] = v
                
        # 2. os.environ에 있는 값을 병합 (런타임 오버라이드 지원)
        for k, v in os.environ.items():
            if any(k.startswith(p) for p in _MANAGED_PREFIXES):
                # 파일에서 읽은 값과 다를 경우 덮어씀 (단, 한글 키가 깨진 경우는 무시되도록 함)
                self._data[k] = v

    def _ensure_integrated_strategy(self):
        """기본 AI 전략(INTEGRATED)이 .env에 없으면 자동 생성"""
        if not self.has_section('INTEGRATED'):
            self.logger.info("기본 AI 전략(INTEGRATED)이 누락되어 자동 생성합니다.")
            import json
            
            # 매수 전략 세팅 (AI 모델에 전적으로 의존)
            self.set('INTEGRATED', 'buy_stg_0', json.dumps({"name": "AI 정밀 매수", "content": "AI_SCORE > 0.75"}, ensure_ascii=False))
            
            # 매도 전략 세팅
            self.set('INTEGRATED', 'sell_stg_0', json.dumps({"name": "AI 조기 매도", "content": "AI_SCORE < 0.3 and current_profit_pct < -1.0", "partial_sell_ratio": 1.0}, ensure_ascii=False))
            self.set('INTEGRATED', 'sell_stg_1', json.dumps({"name": "수익 보존(익절)", "content": "current_profit_pct > 3.0", "partial_sell_ratio": 1.0}, ensure_ascii=False))
            self.set('INTEGRATED', 'sell_stg_2', json.dumps({"name": "기계적 손절", "content": "current_profit_pct < -2.0", "partial_sell_ratio": 1.0}, ensure_ascii=False))
            
            # 전략 목록에 등록
            self.set('STRATEGIES', 'stg_integrated', 'INTEGRATED')
            self.save()

    def read(self, filenames, encoding='utf-8'):
        """filenames 인자는 무시하고 .env 파일을 로드"""
        load_dotenv(self.env_path, override=True, encoding='utf-8')
        self._sync_from_env()
        return [self.env_path]

    def _get_key(self, section, option):
        """섹션과 옵션을 조합하여 .env 키 생성"""
        # 전략 섹션(한글) 대응을 위해 prefix 조정
        if section.upper() in _STANDARD_SECTIONS:
            return f"{section.upper()}_{option.upper()}"
        else:
            # 개별 전략 섹션 (예: [급등주])
            return f"STRATEGY_{section}_{option.upper()}"

    def get(self, section, option, fallback=None):
        key = self._get_key(section, option)
        val = os.environ.get(key, self._data.get(key, fallback))
        # fallback이 None인 경우 ConfigParser는 NoOptionError를 내뱉지만, 여기선 None 반환
        return val

    def getboolean(self, section, option, fallback=False):
        val = self.get(section, option, str(fallback))
        if isinstance(val, bool): return val
        if val is None: return fallback
        return val.lower() in ('true', '1', 't', 'y', 'yes')

    def getint(self, section, option, fallback=0):
        val = self.get(section, option)
        if val is None: return fallback
        try:
            return int(val)
        except (ValueError, TypeError):
            return fallback

    def getfloat(self, section, option, fallback=0.0):
        val = self.get(section, option)
        if val is None: return fallback
        try:
            return float(val)
        except (ValueError, TypeError):
            return fallback

    def has_section(self, section):
        if section.upper() in _STANDARD_SECTIONS:
            prefix = f"{section.upper()}_"
        else:
            prefix = f"STRATEGY_{section}_"
        return any(k.startswith(prefix) for k in self._data)

    def has_option(self, section, option):
        key = self._get_key(section, option)
        return key in self._data or key in os.environ

    def options(self, section):
        """특정 섹션에 속하는 옵션 목록 반환"""
        if section.upper() in _STANDARD_SECTIONS:
            prefix = f"{section.upper()}_"
        else:
            prefix = f"STRATEGY_{section}_"
        
        opts = []
        for k in self._data:
            if k.startswith(prefix):
                opts.append(k[len(prefix):].lower())
        return list(set(opts))

    def set(self, section, option, value):
        """값 설정 및 메모리/환경변수 업데이트"""
        key = self._get_key(section, option)
        str_val = str(value)
        os.environ[key] = str_val
        self._data[key] = str_val

    def items(self, section):
        """특정 섹션의 (키, 값) 튜플 리스트 반환"""
        if section.upper() in _STANDARD_SECTIONS:
            prefix = f"{section.upper()}_"
        else:
            prefix = f"STRATEGY_{section}_"
            
        result = []
        for k, v in self._data.items():
            if k.startswith(prefix):
                result.append((k[len(prefix):].lower(), v))
        return result

    def __getitem__(self, section):
        """config['SECTION'] 형태로 접근할 때 해당 섹션의 딕셔너리 반환"""
        return dict(self.items(section))

    def remove_option(self, section, option):
        """특정 섹션의 옵션을 삭제 (메모리 및 환경변수에서 제거)"""
        key = self._get_key(section, option)
        removed = False
        if key in self._data:
            del self._data[key]
            removed = True
        if key in os.environ:
            del os.environ[key]
            removed = True
        return removed

    def save(self):
        """현재 변경된 설정을 .env 파일에 안전하게 저장 (코멘트 보존, Docker mount 안전)"""
        try:
            with open(self.env_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            updated_keys = set()
            new_lines = []
            
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    new_lines.append(line)
                    continue
                    
                if '=' in stripped:
                    k = stripped.split('=', 1)[0].strip()
                    is_managed = any(k.startswith(p) for p in _MANAGED_PREFIXES)
                    if is_managed:
                        if k in self._data:
                            new_lines.append(f"{k}={self._data[k]}\n")
                            updated_keys.add(k)
                        else:
                            # self._data에 없는 관리 대상 키는 .env 파일에서 삭제 처리
                            pass
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)
            
            for k, v in self._data.items():
                if any(k.startswith(p) for p in _MANAGED_PREFIXES) and k not in updated_keys:
                    new_lines.append(f"{k}={v}\n")
                    
            # os.replace 대신 동일 파일 핸들에 직접 덮어쓰기 (Docker Volume 에러 방지)
            with open(self.env_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
                
        except Exception as e:
            self.logger.error(f"설정 파일 덮어쓰기 실패: {e}")

    def write(self, fp):
        """파일 객체에 설정 내용을 쓰기 (ConfigParser 호환용, 가급적 save() 사용 권장)"""
        lines = []
        for k in sorted(self._data.keys()):
            if any(k.startswith(p) for p in _MANAGED_PREFIXES):
                v = self._data[k]
                lines.append(f"{k}={v}\n")
        
        content = "".join(lines)
        if hasattr(fp, 'write'):
            fp.write(content)

    def add_section(self, section):
        """ConfigParser 호환용 (실제 .env에선 섹션 구분이 없으므로 무시)"""
        pass

    def save_config(self):
        """save() 메서드의 별칭 (웹 대시보드 호환용)"""
        self.save()

    def reload(self):
        """강제로 .env를 다시 로드 (설정이 외부에서 변경된 경우)"""
        load_dotenv(self.env_path, override=True)
        self._data.clear()
        self._sync_from_env()


def get_config():
    """싱글톤 EnvConfigParser 인스턴스를 반환하는 편의 함수"""
    return EnvConfigParser()
