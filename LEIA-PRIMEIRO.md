# WorkPulse — Guia de Deploy na AWS

Este kit coloca o **WorkPulse no ar na sua conta AWS**. Foi montado para você
executar mesmo sem ser técnico: você vai copiar e colar alguns comandos dentro
de um terminal que roda no próprio site da AWS (o **CloudShell**). Não precisa
instalar nada no seu computador.

Leia este guia até o fim **uma vez** antes de começar. O processo todo leva de
**30 a 50 minutos**, sendo que a maior parte é só esperar a AWS criar os
recursos.

---

## 1. O que este kit cria

- **Servidor da API** (ECS Fargate) — o cérebro do WorkPulse, que recebe os
  dados dos agentes e responde aos dashboards.
- **Banco de dados** (SQLite em um disco de rede EFS) — os dados ficam
  guardados de forma permanente e com backup automático diário.
- **Site institucional e dashboards** (S3 + CloudFront) — as páginas
  `index.html`, `admin.html`, `workforce.html`, etc.
- **HTTPS com domínio próprio** — `https://workpulse.com.br` e
  `https://api.workpulse.com.br`, com certificado gratuito da AWS.
- **Monitoramento** — alertas por e-mail se algo der errado.

Toda a infraestrutura é criada por código (Terraform). Se um dia quiser
remover tudo, há um script que apaga em um comando só.

---

## 2. Quanto vai custar

Estimativa para um começo de operação, na região Norte da Virgínia (us-east-1):

| Item | Custo aproximado/mês |
|---|---|
| Servidor (ECS Fargate, 1 instância) | US$ 18 |
| Load Balancer (ALB) | US$ 19 |
| NAT Gateway | US$ 33 |
| Disco EFS + backups | US$ 1 |
| CloudFront + S3 (site) | US$ 3 |
| CloudWatch (logs/monitor) | US$ 5 |
| Secrets Manager + Route 53 + ECR | US$ 2 |
| **Total estimado** | **~US$ 80 a 120/mês** |

O maior item isolado é o NAT Gateway (~US$ 33). Dá para reduzir isso no futuro;
não se preocupe com isso agora. Os custos só começam quando você roda o deploy.

---

## 3. Antes de começar você precisa de

1. **Acesso à sua conta AWS** (o login `caiocesarb@gmail.com`).
2. **O domínio `workpulse.com.br`** com uma *hosted zone* no Route 53 da AWS
   (a Parte 5 explica como conferir e criar, se ainda não existir).
3. **Este kit** — a pasta `workpulse-deploy` (ou o arquivo
   `workpulse-deploy.zip`) que acompanha este guia.

Nada precisa ser instalado no seu computador.

---

## 4. Parte 1 — Segurança da conta AWS (faça isto primeiro)

> **Importante.** A senha da sua conta AWS estava num arquivo de texto comum
> (`AWS-DADOS.txt`) e foi compartilhada em conversa. Por segurança, trate essa
> senha como **exposta** e corrija isto antes de qualquer coisa.

1. Acesse <https://console.aws.amazon.com/> e entre com o e-mail
   `caiocesarb@gmail.com` e a senha atual.
2. No canto superior direito, clique no seu nome → **Security credentials**
   (Credenciais de segurança).
3. **Troque a senha:** procure a opção de alterar a senha da conta-raiz e
   defina uma **senha nova, longa e única** (use um gerenciador de senhas).
4. **Ative o MFA (autenticação em duas etapas):** na mesma página, em
   *Multi-factor authentication (MFA)*, clique em **Assign MFA device** e siga
   o passo a passo (o app *Google Authenticator* ou *Authy* no celular serve).
5. **Apague ou proteja o arquivo `AWS-DADOS.txt`** do seu computador — ele não
   é mais necessário e não deve ficar guardado em texto puro.

Isso é suficiente para fazer o deploy com segurança. Para o uso do dia a dia,
o ideal (mais à frente) é criar um *usuário IAM* separado em vez de usar a
conta-raiz — veja a Parte 9. Mas **não é obrigatório agora**: o deploy pode ser
feito normalmente pelo CloudShell já logado.

---

## 5. Parte 2 — Confirmar o domínio no Route 53

O deploy precisa que o domínio `workpulse.com.br` tenha uma **hosted zone** no
Route 53. Vamos confirmar.

1. No Console da AWS, no campo de busca no topo, digite **Route 53** e abra.
2. No menu à esquerda, clique em **Hosted zones**.
3. Procure por `workpulse.com.br` na lista.

**Se aparecer:** ótimo, pode pular para a Parte 3.

**Se NÃO aparecer**, você precisa criar a zona e apontar o domínio para ela:

1. Clique em **Create hosted zone**, digite `workpulse.com.br`, tipo
   *Public hosted zone*, e crie.
2. Abra a zona criada. Você verá 4 endereços do tipo `ns-xxxx.awsdns-xx.org`
   no registro **NS**. Copie os 4.
3. Como `.com.br` é registrado no **Registro.br**, entre em
   <https://registro.br/>, acesse o domínio `workpulse.com.br` e, na
   configuração de DNS, escolha **usar outros servidores DNS** e cole os 4
   nameservers da AWS.
4. **Aguarde a propagação** — pode levar de 30 minutos a algumas horas. Só
   continue para a Parte 3 depois disso, senão o certificado HTTPS não valida.

> O script de bootstrap (Parte 3) também avisa se a zona não for encontrada.

---

## 6. Parte 3 — Subir o projeto

Agora o deploy de fato. Tudo acontece no **AWS CloudShell**, um terminal que
roda dentro do navegador, no próprio site da AWS.

### Passo 1 — Abrir o CloudShell

1. Confira no canto superior direito do Console se a região é
   **N. Virginia (us-east-1)**. Se não for, troque para ela.
2. Clique no ícone de terminal (`>_`) no topo do Console, ao lado do sino de
   notificações. (Ou busque por **CloudShell**.)
3. Espere o terminal abrir — na primeira vez leva cerca de 1 minuto.

### Passo 2 — Enviar o kit para o CloudShell

1. No CloudShell, clique no menu **Actions** (canto superior direito do
   terminal) → **Upload file**.
2. Selecione o arquivo **`workpulse-deploy.zip`** (está na mesma pasta deste
   guia, no seu computador).
3. Depois que terminar o envio, no terminal digite:

   ```bash
   unzip workpulse-deploy.zip
   cd workpulse-deploy
   ```

### Passo 3 — Rodar as 3 etapas

Rode os comandos abaixo **um de cada vez**, esperando cada um terminar antes
de passar para o próximo.

```bash
bash scripts/1-bootstrap.sh
```

Prepara os recursos básicos. Leva menos de 1 minuto.

```bash
bash scripts/2-deploy.sh
```

Cria toda a infraestrutura e constrói o servidor. **Leva de 15 a 25 minutos.**
É normal parecer parado em alguns momentos (a AWS está criando o certificado
e o CloudFront). Ao final, ele mostra a URL da API e o **token de admin** —
copie esse token e guarde em local seguro.

```bash
bash scripts/3-publicar-site.sh
```

Publica o site e os dashboards. Leva cerca de 1 minuto.

### Passo 4 — Confirmar o e-mail de alertas

A AWS envia um e-mail para **caiob@hotmail.com** com o assunto
*"AWS Notification - Subscription Confirmation"*. Abra-o e clique em
**Confirm subscription** para passar a receber os alertas de monitoramento.

Pronto — o deploy está feito. ✅

---

## 7. Parte 4 — Validar que está tudo no ar

Logo após o deploy, o DNS e o CloudFront ainda estão se propagando. **Espere
de 15 a 40 minutos** e então teste:

1. **API:** abra `https://api.workpulse.com.br/healthz` no navegador. Deve
   aparecer algo como `{"status":"ok","version":"1.0.0"}`.
2. **Site:** abra `https://workpulse.com.br` — deve carregar a página
   institucional do WorkPulse.
3. **Dashboard de administração:** abra `https://workpulse.com.br/admin.html`.
   No formulário de login, o campo de URL já vem preenchido com
   `https://api.workpulse.com.br`. Cole o **token de admin** (o que o script
   da etapa 2 mostrou) e entre.

Se algo não abrir de primeira, aguarde mais um pouco — propagação de DNS leva
tempo. Veja também a Parte 8 (Problemas comuns).

---

## 8. Parte 5 — Como usar o WorkPulse

O servidor sobe com o **banco vazio** — sem clientes nem licenças ainda.

- **Token de admin:** é a senha-mestra dos dashboards. Se precisar vê-lo de
  novo, no CloudShell rode, dentro de `workpulse-deploy/infra`:
  `terraform output -raw admin_token`
- **Dashboards disponíveis:**
  - `https://workpulse.com.br/admin.html` — administração da plataforma
  - `https://workpulse.com.br/workforce.html` — visão de força de trabalho
  - `https://workpulse.com.br/dashboard/index.html` — painel geral
- **Emitir a primeira licença:** pelo dashboard de administração, na seção de
  licenças, ou pela API. Cada empresa-cliente (tenant) recebe uma licença que
  os agentes usam para se autenticar.
- **Instalar os agentes nas estações de trabalho:** isso é um processo à parte,
  descrito no documento `MDM_GUIDE.md` e na Parte 2 do runbook original
  `04_WorkPulse_Deploy_AWS.md`. O agente aponta para `https://api.workpulse.com.br`.

---

## 9. Parte 6 — Manutenção

Todos os comandos abaixo são rodados no **CloudShell**, dentro da pasta
`workpulse-deploy`.

- **Atualizar o servidor** (depois de mudar `server/main.py`): reenvie o kit
  atualizado e rode `bash scripts/redeploy.sh`. Reconstrói a imagem e troca o
  servidor (alguns segundos de indisponibilidade).
- **Publicar mudanças no site:** rode `bash scripts/3-publicar-site.sh`.
- **Ver os logs do servidor:** Console AWS → **CloudWatch** → *Log groups* →
  `/aws/ecs/workpulse-prod-server`.
- **Ver o estado do servidor:** Console AWS → **ECS** → cluster
  `workpulse-prod` → serviço `workpulse-prod-server`.
- **Destruir tudo** (para parar de gerar custos): rode
  `bash scripts/destruir.sh` e digite `DESTRUIR` para confirmar. Atenção: isso
  **apaga os dados** do banco.

---

## 10. Parte 7 — Problemas comuns

- **"O script 2 falhou no certificado / ficou preso muito tempo."** A *hosted
  zone* do domínio provavelmente não está ativa, ou os nameservers no
  Registro.br ainda não propagaram. Confira a Parte 2, aguarde, e rode
  `bash scripts/2-deploy.sh` de novo (ele continua de onde parou).
- **"O site abre, mas dá erro 404."** O CloudFront ainda está propagando.
  Aguarde de 15 a 40 minutos. Se persistir, rode `bash scripts/3-publicar-site.sh`.
- **"A API responde mas o servidor caiu."** Veja os logs em CloudWatch
  (`/aws/ecs/workpulse-prod-server`). Você também recebe um e-mail de alerta.
- **"O build da imagem falhou."** Console AWS → **CodeBuild** → projeto
  `workpulse-prod-image-build` → veja o log da última execução.
- **Qualquer script pode ser rodado de novo** com segurança — eles continuam
  de onde pararam, não duplicam recursos.

---

## 11. Parte 8 — Recomendações e próximos passos

- **Usuário IAM em vez da conta-raiz (recomendado):** para o uso do dia a dia,
  crie em *IAM → Users* um usuário com a permissão `AdministratorAccess`,
  com MFA, e use-o no lugar da conta-raiz. Guarde a conta-raiz apenas para
  emergências.
- **Escalar para alta disponibilidade:** este deploy roda em 1 instância
  (limite do banco SQLite). Para suportar mais carga e ter alta
  disponibilidade real, o próximo passo é portar o servidor para PostgreSQL —
  posso fazer isso quando você quiser.
- **CI/CD automático:** dá para conectar um repositório GitHub para que cada
  atualização do código publique sozinha. Não está incluído neste kit.
- **Reduzir custo:** o NAT Gateway (~US$ 33/mês) pode ser substituído por
  *VPC Endpoints* mais adiante, economizando boa parte desse valor.

---

## 12. Resumo dos comandos

```bash
# No AWS CloudShell, região us-east-1:
unzip workpulse-deploy.zip
cd workpulse-deploy
bash scripts/1-bootstrap.sh        # ~1 min
bash scripts/2-deploy.sh           # ~15-25 min
bash scripts/3-publicar-site.sh    # ~1 min
```

Qualquer dúvida ou erro, me mande a mensagem que o terminal mostrou e eu
te ajudo a resolver.
