# Ditado inteligente para Windows

MVP em Python que combina o ditado nativo do Windows 11 (`Win + H`) com a reescrita de texto pela API da OpenAI. O programa envia somente o texto já transcrito pelo Windows: não grava nem envia áudio.

## Requisitos

- Windows 11;
- Python 3.11 ou superior;
- ditado por voz do Windows configurado;
- uma chave da API da OpenAI.

O Tkinter acompanha a instalação padrão do Python para Windows.

## Instalação

Abra o PowerShell nesta pasta e crie um ambiente virtual:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Se a política do PowerShell bloquear a ativação, execute uma vez na sessão:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Instale as dependências:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Configuração

Edite o arquivo `.env` e informe sua chave, sem aspas:

```dotenv
OPENAI_API_KEY=sk-sua-chave-aqui
OPENAI_TEXT_MODEL=gpt-5.4-mini
```

O `.env` está no `.gitignore` para reduzir o risco de publicar a chave. Nunca envie esse arquivo a outras pessoas nem grave a chave diretamente no código.

`OPENAI_TEXT_MODEL` define o modelo usado. O valor incluído é apenas um padrão; altere-o se esse modelo não estiver disponível na sua conta ou se quiser usar outro modelo de texto compatível com a Responses API.

Também é possível definir as variáveis apenas na sessão atual do PowerShell:

```powershell
$env:OPENAI_API_KEY = "sk-sua-chave-aqui"
$env:OPENAI_TEXT_MODEL = "gpt-5.4-mini"
```

## Execução e uso

Com o ambiente virtual ativado, execute:

```powershell
python main.py
```

O terminal exibirá `Aguardando atalho Ctrl + Alt + M...`.

1. Clique no campo do programa em que deseja inserir a mensagem.
2. Pressione `Ctrl + Alt + M`.
3. A janela intermediária será aberta e o programa enviará `Win + H` automaticamente.
4. Dite o texto. O próprio Windows colocará a transcrição na caixa.
5. Pressione `Ctrl + Enter` ou clique em **Reescrever e copiar**.
6. Um painel central com **Texto copiado! Pressione Ctrl + V.** será exibido por um segundo antes de a janela fechar.
7. Volte ao campo desejado e pressione `Ctrl + V` para colar.

Se não quiser usar a API, clique em **Copiar texto bruto**. O conteúdo da caixa será copiado sem alterações e a janela fechará imediatamente.

Pressione `Esc` ou clique em **Cancelar** para fechar sem copiar. Se o painel de digitação por voz não abrir automaticamente, confirme nas configurações do Windows se o reconhecimento de fala online está habilitado e teste `Win + H` manualmente em uma caixa de texto.

## Comandos falados de estrutura

Durante o ditado, use estas frases explícitas:

- `comando nova linha`: insere uma quebra de linha no texto tratado;
- `comando novo parágrafo`: insere duas quebras, deixando uma linha em branco.

Os comandos não precisam ser reconhecidos pelo Windows como ações: mesmo que apareçam escritos na caixa, a aplicação os converte ao selecionar **Reescrever e copiar**. Maiúsculas e minúsculas não fazem diferença, e `paragrafo` sem acento também é aceito. Pontuação simples logo depois do comando é removida para não aparecer solta na nova linha.

O botão **Copiar texto bruto** não interpreta esses comandos e copia as frases literalmente.

## Atalho global

O atalho é registrado diretamente pela API do Windows. Caso outra aplicação já use a mesma combinação, o programa encerrará com uma mensagem clara no terminal. Se o atalho não for registrado:

- confira se outro programa já usa `Ctrl + Alt + M`;
- escolha outra combinação na constante abaixo;
- mantenha o terminal aberto enquanto usa o ditado.

Executar como administrador não deve ser necessário para capturar o atalho. A colagem é feita manualmente pelo usuário.

## Instância única

Somente uma instância do programa pode permanecer ativa por sessão do Windows. Se `main.py` for executado novamente enquanto o ditado já estiver aberto, a nova execução exibirá **O ditado inteligente já está em execução** e será encerrada. A instância original continuará funcionando normalmente.

Para trocar o atalho, altere esta constante no início de `main.py`:

```python
MAIN_HOTKEY = "ctrl+alt+m"
```

São aceitos os modificadores `ctrl`, `alt`, `shift` e `win`, combinados com uma letra, número ou tecla de `F1` a `F24`. Exemplo: `ctrl+shift+d`.

## Prompt de edição

O comportamento da reescrita fica em `prompts/editor_mensagens.txt`. É possível editar esse arquivo sem alterar o código. A resposta da API é usada como texto final e deve conter somente a mensagem reescrita.

## Solução de problemas

- **Detalhes para diagnóstico:** as falhas capturadas exibem o traceback completo na caixa de erro e no terminal. Falhas ocorridas durante a inicialização são exibidas no terminal.
- **Chave ausente:** preencha `OPENAI_API_KEY` no `.env` e reinicie o programa.
- **Texto vazio:** dite ou digite algo antes de confirmar.
- **Falha da API:** confira a chave, o modelo, a conexão e os limites da conta. O texto bruto permanece na janela.
- **Falha ao copiar:** a aplicação usa diretamente o clipboard do Tkinter e tenta novamente sem bloquear a interface. Se as tentativas falharem, o texto bruto permanece na janela e o traceback completo é exibido.
- **Atalho não registrado:** feche o programa que usa a mesma combinação ou escolha outro atalho.
- **Aplicativo já em execução:** use a instância que já está aguardando o atalho; não é necessário abrir outra.

Para encerrar, pressione `Ctrl + C` no terminal ou feche o terminal.
