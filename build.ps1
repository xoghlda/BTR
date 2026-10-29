$ErrorActionPreference = 'Stop'
$workspaceRoot = $PSScriptRoot
Push-Location -LiteralPath $workspaceRoot
try {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw 'Python 3.12를 설치한 뒤 python -m pip install -r requirements-dev.txt를 실행해 주세요.'
    }
    $versionOutput = & python -c "from trip_capture import __version__; print(__version__)"
    if ($LASTEXITCODE -ne 0) {
        throw 'trip_capture.__version__을 읽지 못했습니다.'
    }
    $appVersion = ($versionOutput | Out-String).Trim()
    if ($appVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw '프로그램 버전은 x.y.z 형식이어야 합니다. 예: 1.4.0'
    }
    $executableName = '출장증빙캡처_v' + $appVersion
    $buildArguments = @(
        '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--windowed', '--onefile',
        '--name', $executableName,
        '--collect-all', 'playwright',
        '--collect-all', 'pypdfium2',
        '--collect-all', 'pypdfium2_raw',
        '--distpath', (Join-Path $workspaceRoot 'dist'),
        '--workpath', (Join-Path $workspaceRoot 'build'),
        '--specpath', $workspaceRoot,
        (Join-Path $workspaceRoot 'main.py')
    )
    & python @buildArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'EXE 빌드에 실패했습니다. 의존성을 설치하고 위 오류를 확인해 주세요: python -m pip install -r requirements-dev.txt'
    }
    $executablePath = Join-Path (Join-Path $workspaceRoot 'dist') ($executableName + '.exe')
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw '빌드는 끝났지만 예상한 EXE 파일을 찾지 못했습니다.'
    }
    Write-Host ('완성 파일: ' + $executablePath)
}
finally {
    Pop-Location
}
