# Orientações para agentes de IA

## Escopo

Este arquivo se aplica a todo o repositório. O projeto é um MVP exclusivo para Windows 11, escrito em Python 3.11+, que combina o ditado nativo do Windows com edição textual pela API da OpenAI.

## Comportamento que deve ser preservado

O fluxo atual é:

1. O processo fica aguardando o atalho global definido em `MAIN_HOTKEY`.
2. O atalho abre uma janela Tkinter e aciona `Win + H`.
3. O Windows transcreve a fala diretamente na caixa de texto.
4. O texto, e somente o texto, é enviado à Responses API da OpenAI.
5. O resultado é copiado para a área de transferência.
6. Uma confirmação visual permanece por um segundo e a janela fecha.
7. O usuário cola manualmente com `Ctrl + V`.

O botão `Copiar texto bruto` é um caminho alternativo: copia exatamente o conteúdo da caixa, sem chamar a API, e fecha imediatamente.

No fluxo reescrito, `comando nova linha` produz `\n` e `comando novo parágrafo` produz `\n\n`. O fluxo bruto deve preservar essas frases literalmente.

Não grave áudio, não envie áudio à OpenAI e não implemente restauração de foco ou colagem automática sem solicitação explícita. A decisão de deixar o texto apenas na área de transferência é intencional.

## Arquivos importantes

- `main.py`: aplicação, interface, atalho nativo, chamada à API e cópia para o clipboard.
- `prompts/editor_mensagens.txt`: instruções fixas enviadas ao modelo.
- `requirements.txt`: dependências de execução.
- `.env.example`: nomes e valores de exemplo das variáveis de ambiente.
- `README.md`: instalação, operação e solução de problemas para o usuário.

## Arquitetura e cuidados técnicos

- O atalho global usa `RegisterHotKey` da Win32 em uma thread com fila de mensagens. Preserve a liberação com `UnregisterHotKey` e `WM_QUIT`.
- O aplicativo usa um mutex Win32 nomeado e local à sessão para garantir instância única. Preserve o handle durante toda a execução e libere-o em todos os caminhos de saída.
- A thread do atalho e a thread da API não devem manipular widgets Tkinter diretamente. Elas devem publicar eventos em `UI_EVENTS`; `_poll_ui_events()` executa as mudanças na thread da interface.
- A chamada à OpenAI deve continuar fora da thread da interface para não congelar a janela.
- `operation_id` invalida respostas tardias depois de um cancelamento. Preserve essa proteção ao alterar o fluxo assíncrono.
- Aguarde a liberação dos modificadores do atalho antes de enviar `Win + H`; caso contrário, o Windows pode receber uma combinação diferente.
- A integração com a OpenAI usa `client.responses.create()`, com o prompt em `instructions` e o texto ditado em `input`. O modelo vem de `OPENAI_TEXT_MODEL`.
- Antes da API, os comandos falados de estrutura viram os marcadores `[[DITADO_NOVA_LINHA]]` e `[[DITADO_NOVO_PARAGRAFO]]`. O prompt deve exigir sua preservação exata e a resposta deve restaurá-los localmente para `\n` e `\n\n`.
- Em falhas da API ou do clipboard, mantenha o texto bruto na janela para nova tentativa.
- Durante a chamada da API, desabilite também o botão de cópia bruta. Após sucesso da API, preserve o painel central de confirmação por 1.000 ms; a cópia bruta deve fechar imediatamente.

## Segurança e dados sensíveis

- Nunca grave chaves no código, no README, em testes ou em logs.
- Não leia, exiba, copie, sobrescreva ou inclua `.env` em commits. Para diagnósticos, verifique apenas se a variável existe, sem imprimir seu valor.
- `.env` deve continuar ignorado pelo Git; atualize `.env.example` quando novas variáveis forem introduzidas.
- Não faça chamadas reais/pagas à OpenAI durante testes sem autorização explícita. Use mocks.

## Estilo e documentação

- Mantenha o código modular, com type hints e funções pequenas.
- Preserve textos de interface e mensagens ao usuário em português.
- Salve arquivos textuais em UTF-8.
- Ao alterar comportamento, atalho, modelo padrão, dependências ou configuração, atualize também o `README.md` e, quando aplicável, `.env.example`.
- Não adicione ícone de bandeja ou interface complexa sem solicitação; este projeto continua sendo um MVP.

## Instalação e execução

No PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

O Tkinter faz parte da instalação padrão do Python para Windows.

## Verificação antes de concluir mudanças

Execute pelo menos:

```powershell
.\.venv\Scripts\python.exe -m compileall main.py
.\.venv\Scripts\python.exe -m pip check
```

Ainda não há uma suíte de testes persistente. Para alterações relevantes:

- simule `OpenAI` e `response.output_text`, sem rede;
- simule `pyperclip.copy`, sem substituir o clipboard do usuário;
- simule `trigger_windows_dictation()` em testes da interface;
- valide criação, cancelamento e fechamento da janela Tkinter;
- valide os dois fluxos de cópia, incluindo a confirmação temporária somente no texto tratado;
- valide codificação e restauração dos comandos falados, incluindo capitalização, espaços, pontuação e `paragrafo` sem acento;
- valide aquisição, conflito e liberação do mutex de instância única;
- valide registro, conflito e encerramento do atalho nativo quando tocar nessa área.

O teste manual final deve ser feito no Windows: atalho global → janela → `Win + H` → reescrita → texto no clipboard. Não afirme que esse fluxo foi validado integralmente se alguma etapa visual ou dependente do sistema não foi realmente exercitada.

## Higiene do repositório

O diretório de trabalho pode conter mudanças do usuário. Não use `git reset --hard`, `git checkout --` ou comandos equivalentes para descartá-las. Preserve alterações não relacionadas e revise o diff antes de finalizar.
