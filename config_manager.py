import os
import io
import logging
from dotenv import load_dotenv, set_key, find_dotenv

# 프로젝트에서 관리하는 .env 키 접두사 목록
_MANAGED_PREFIXES = (
    'STRATEGIES_', 'BUYCOUNT_', 'TRADING_', 'DATA_SAVING_', 'CHART_',
    'KIWOOM_API_', 'LOGIN_', 'SLACK_', 'SETTINGS_', 'STRATEGY_', 'API_', 'SYSTEM_'
)

# 표준 섹션 이름 (대문자)
_STANDARD_SECTIONS = frozenset([
    'STRATEGIES', 'BUYCOUNT', 'TRADING', 'DATA_SAVING', 'CHART',
    'KIWOOM_API', 'LOGIN', 'SLACK', 'SETTINGS', 'API', 'SYSTEM'
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
        
        self.sanitize_env_file()
        load_dotenv(self.env_path, override=True, encoding='utf-8-sig')
        self._data = {}
        self._sync_from_env()

    def _sync_from_env(self):
        """환경 변수 및 파일에서 캐시로 데이터 동기화 (관리 대상 키만)"""
        from dotenv import dotenv_values
        file_env = {}
        try:
            file_env = dotenv_values(self.env_path, encoding='utf-8-sig')
        except Exception:
            pass

        # dotenv_values가 실패하거나 누락된 경우를 대비한 수동 파싱 (fallback)
        if os.path.exists(self.env_path):
            try:
                with open(self.env_path, 'r', encoding='utf-8-sig') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            k, v = line.split('=', 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k not in file_env:
                                file_env[k] = v
            except Exception:
                pass
        
        # 1. 파일에서 읽은 값을 우선적으로 딕셔너리에 저장 (한글 키 깨짐 방지 및 BOM 제거)
        for k, v in file_env.items():
            clean_k = k.lstrip('\ufeff')
            if v is not None and any(clean_k.upper().startswith(p) for p in _MANAGED_PREFIXES):
                self._data[clean_k] = v
                
        # 2. os.environ에 있는 값을 병합 (런타임 오버라이드 지원)
        for k, v in os.environ.items():
            if any(k.startswith(p) for p in _MANAGED_PREFIXES):
                # 파일에 값이 아예 없거나, 파일 값이 비어있고 os.environ 값은 있을 때만 사용
                if k not in self._data or (not self._data[k] and v):
                    self._data[k] = v


    def read(self, filenames, encoding='utf-8'):
        """filenames 인자는 무시하고 .env 파일을 로드"""
        load_dotenv(self.env_path, override=True, encoding='utf-8-sig')
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
        # 1. Exact match
        val = self._data.get(key, os.environ.get(key))
        if val is not None:
            return val
            
        # 2. Case-insensitive match (for manual .env edits like SETTINGS_prime_cash)
        key_upper = key.upper()
        for k, v in self._data.items():
            if k.upper() == key_upper:
                return v
        for k, v in os.environ.items():
            if k.upper() == key_upper:
                return v
                
        return fallback

    def getboolean(self, section, option, fallback=False):
        val = self.get(section, option, str(fallback))
        if isinstance(val, bool): return val
        if val is None: return fallback
        return val.lower() in ('true', '1', 't', 'y', 'yes')

    def getint(self, section, option, fallback=0):
        val = self.get(section, option)
        if val is None: return fallback
        try:
            if isinstance(val, str):
                val = val.replace(',', '').strip('"').strip("'")
            return int(val)
        except (ValueError, TypeError):
            return fallback

    def getfloat(self, section, option, fallback=0.0):
        val = self.get(section, option)
        if val is None: return fallback
        try:
            if isinstance(val, str):
                val = val.replace(',', '').strip('"').strip("'")
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

    def sanitize_env_file(self):
        """기존 .env 파일 내의 '='가 없는 유효하지 않은 잔재 라인(Lines without '=') 자동 청소 및 단일 행 변환"""
        try:
            if not os.path.exists(self.env_path):
                return
            with open(self.env_path, 'r', encoding='utf-8-sig') as f:
                lines = f.readlines()
            
            clean_lines = []
            has_invalid = False
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    clean_lines.append(line)
                elif '=' in stripped:
                    k, v = stripped.split('=', 1)
                    k = k.strip()
                    v = v.strip().replace('\r\n', ' ').replace('\n', ' ')
                    clean_lines.append(f"{k}={v}\n")
                else:
                    has_invalid = True
                    
            if has_invalid:
                with open(self.env_path, 'w', encoding='utf-8') as f:
                    f.writelines(clean_lines)
                self.logger.info("🧹 .env 파일 내 손상된 다중 행 잔재 라인을 자동으로 청소 정문화했습니다.")
        except Exception as e:
            self.logger.error(f"❌ .env 자동 청소 실패: {e}")

    def save(self):
        """현재 변경된 설정을 .env 파일에 안전하게 저장 (코멘트 보존, Docker mount 안전)"""
        try:
            self.sanitize_env_file()
            
            with open(self.env_path, 'r', encoding='utf-8-sig') as f:
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
                            val = str(self._data[k]).replace('\r\n', ' ').replace('\n', ' ')
                            new_lines.append(f"{k}={val}\n")
                            updated_keys.add(k)
                        else:
                            pass
                    else:
                        new_lines.append(line)
            
            for k, v in self._data.items():
                if any(k.startswith(p) for p in _MANAGED_PREFIXES) and k not in updated_keys:
                    val = str(v).replace('\r\n', ' ').replace('\n', ' ')
                    new_lines.append(f"{k}={val}\n")
                    
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

    def get_trading_time_settings(self):
        """초단타 트레이딩 시간 필터 설정 반환"""
        buy_end_time_str = self.get('TRADING', 'buy_end_time', '15:00')
        sell_all_time_str = self.get('TRADING', 'sell_all_time', '15:00')
        sell_all_enabled = self.getboolean('TRADING', 'sell_all_enabled', True)
        
        import datetime
        try:
            buy_end_time = datetime.datetime.strptime(buy_end_time_str, "%H:%M").time()
        except Exception:
            buy_end_time = datetime.time(15, 0)
            
        try:
            sell_all_time = datetime.datetime.strptime(sell_all_time_str, "%H:%M").time()
        except Exception:
            sell_all_time = datetime.time(15, 0)
            
        return {
            'buy_end_time': buy_end_time,
            'sell_all_time': sell_all_time,
            'sell_all_enabled': sell_all_enabled
        }

def get_config():
    """싱글톤 EnvConfigParser 인스턴스를 반환하는 편의 함수"""
    return EnvConfigParser()
