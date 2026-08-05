"""라이선스 발급기.

**고객사에 배포되지 않는다.** 이 패키지는 공급사 발급 서버에만 설치하고,
개인키는 그 서버 밖으로 나가지 않는다. 제품 이미지에 함께 들어가는 순간
고객사가 무제한 라이선스를 스스로 발급할 수 있게 된다.
"""

from licgen.issuer import IssuedLicense, generate_key_pair, issue

__all__ = ["IssuedLicense", "generate_key_pair", "issue"]
