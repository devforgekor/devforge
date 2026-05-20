#!/bin/bash
# 터미널 다크 테마를 영구적으로 적용합니다.
# 1. set_eye_comfort_theme.sh 가 존재하는지 확인
# 2. ~/.bashrc 에 source 명령을 추가 (중복 방지)
# 3. 현재 세션에 즉시 적용

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
THEME_SCRIPT="$SCRIPT_DIR/set_eye_comfort_theme.sh"

if [ ! -f "$THEME_SCRIPT" ]; then
    echo "오류: $THEME_SCRIPT 파일이 없습니다."
    exit 1
fi

BASHRC="$HOME/.bashrc"

# 이미 등록되어 있는지 확인
if grep -q "set_eye_comfort_theme.sh" "$BASHRC" 2>/dev/null; then
    echo "✔ 이미 등록되어 있습니다. (중복 방지)"
else
    # source 명령 추가
    echo "" >> "$BASHRC"
    echo "# 👁️ 어두운 배경 테마 (자동 로드)" >> "$BASHRC"
    echo "source $THEME_SCRIPT" >> "$BASHRC"
    echo "✔ ~/.bashrc 에 등록 완료."
fi

# 현재 세션에 적용
source "$THEME_SCRIPT"

echo ""
echo "이제 터미널 배경이 어두운 테마로 변경됩니다."
echo "새 터미널을 열 때도 자동 적용됩니다."
