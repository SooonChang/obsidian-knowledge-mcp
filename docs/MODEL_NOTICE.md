# 모델 출처

선택적 의미 검색은 intfloat/multilingual-e5-small을 사용합니다.
공식 [모델 저장소](https://huggingface.co/intfloat/multilingual-e5-small/tree/main)는 MIT 라이선스로 표시되어 있습니다 (2026-09-15 확인).

검증에 사용한 원본 리비전: 614241f622f53c4eeff9890bdc4f31cfecc418b3.
원본을 ONNX로 변환하고 MatMul 가중치에 동적 INT8 양자화를 적용했습니다.
모든 가중치가 INT8이 되는 것은 아니므로 운영 모델은 약 407 MB, 토크나이저는 약 17 MB입니다.
변환 결과의 무결성은 모델 디렉터리의 manifest.json에 기록합니다.

모델·원문·검색 DB·인증정보는 Git 관리 대상에서 제외합니다.
준비된 로컬 모델을 다른 운영 장비로 옮길 때 원본 모델 카드와 라이선스 고지도 함께 보관합니다.
