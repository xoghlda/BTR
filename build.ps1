$ErrorActionPreference = 'Stop'
$workspaceRoot = $PSScriptRoot
Push-Location -LiteralPath $workspaceRoot
try {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw 'Python 3.12를 설치한 뒤 python -m pip install -r requirements-dev.txt를 실행해 주세요.'
    }
    $buildArguments = @(
        '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--windowed', '--onefile',
        '--name', '출장증빙캡처_v1.3.1',
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
    $executablePath = Join-Path $workspaceRoot 'dist\출장증빙캡처_v1.3.1.exe'
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw '빌드는 끝났지만 예상한 EXE 파일을 찾지 못했습니다.'
    }
    Write-Host ('완성 파일: ' + $executablePath)
}
finally {
    Pop-Location
}
