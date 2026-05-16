import os
import io
import logging
from dotenv import load_dotenv, set_key, find_dotenv

class EnvConfigParser:
    """ConfigParser와 호환되는 .env 기반 설정 관리자 (Shim 클래스)"""
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.env_path = find_dotenv() or '.env'
        if not os.path.exists(self.env_path):
            with open(self.env_path, 'w', encoding='utf-8') as f:
                f.write("# Generated .env file\n")
        
        load_dotenv(self.env_path, override=True)
        self._data = {}
        self._sync_from_env()

    def _sync_from_env(self):
        """환경 변수에서 캐시로 데이터 동기화"""
        # os.environ의 모든 값을 가져오되, .env에서 로드된 것과 유사한 패턴만 필터링할 수도 있으나
        # 여기선 단순히 전체를 관리 대상으로 둠
        for k, v in os.environ.items():
            self._data[k] = v

    def read(self, filenames, encoding='utf-8'):
        """filenames 인자는 무시하고 .env 파일을 로드"""
        load_dotenv(self.env_path, override=True)
        self._sync_from_env()
        return [self.env_path]

    def _get_key(self, section, option):
        """섹션과 옵션을 조합하여 .env 키 생성"""
        # 전략 섹션(한글) 대응을 위해 prefix 조정
        if section.upper() in ['STRATEGIES', 'BUYCOUNT', 'TRADING', 'DATA_SAVING', 'CHART', 'KIWOOM_API', 'LOGIN', 'SLACK', 'SETTINGS']:
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
        except:
            return fallback

    def getfloat(self, section, option, fallback=0.0):
        val = self.get(section, option)
        if val is None: return fallback
        try:
            return float(val)
        except:
            return fallback

    def has_section(self, section):
        prefix = f"{section.upper()}_" if section.upper() != section else f"STRATEGY_{section}_"
        # 간단하게 체크
        return any(k.startswith(prefix) or k.startswith(f"{section.upper()}_") for k in self._data)

    def has_option(self, section, option):
        key = self._get_key(section, option)
        return key in self._data or key in os.environ

    def options(self, section):
        """특정 섹션에 속하는 옵션 목록 반환"""
        # 섹션별 접두사 정의
        standard_sections = ['STRATEGIES', 'BUYCOUNT', 'TRADING', 'DATA_SAVING', 'CHART', 'KIWOOM_API', 'LOGIN', 'SLACK', 'SETTINGS']
        if section.upper() in standard_sections:
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

    def write(self, fp):
        """파일 객체에 쓰고, .env 파일에도 영구 저장"""
        lines = []
        # 가독성을 위해 정렬하여 저장
        for k in sorted(self._data.keys()):
            # 시스템 환경변수 전체를 쓰지 않고, 관리 대상(접두사가 있는 것들)만 필터링 시도
            if '_' in k:
                v = self._data[k]
                lines.append(f"{k}={v}\n")
                # .env 파일에 즉시 반영
                set_key(self.env_path, k, v)
        
        content = "".join(lines)
        if hasattr(fp, 'write'):
            fp.write(content)

    def add_section(self, section):
        """ConfigParser 호환용 (실제 .env에선 섹션 구분이 없으므로 무시)"""
        pass
