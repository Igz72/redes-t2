import asyncio
import random
import time
from tcputils import *


class Servidor:
    def __init__(self, rede, porta):
        self.rede = rede
        self.porta = porta
        self.conexoes = {}
        self.callback = None
        self.rede.registrar_recebedor(self._rdt_rcv)

    def registrar_monitor_de_conexoes_aceitas(self, callback):
        """
        Usado pela camada de aplicação para registrar uma função para ser chamada
        sempre que uma nova conexão for aceita
        """
        self.callback = callback

    def _rdt_rcv(self, src_addr, dst_addr, segment):
        src_port, dst_port, seq_no, ack_no, \
            flags, window_size, checksum, urg_ptr = read_header(segment)

        if dst_port != self.porta:
            # Ignora segmentos que não são destinados à porta do nosso servidor
            return
        if not self.rede.ignore_checksum and calc_checksum(segment, src_addr, dst_addr) != 0:
            print('descartando segmento com checksum incorreto')
            return

        payload = segment[4*(flags>>12):]
        id_conexao = (src_addr, src_port, dst_addr, dst_port)

        if (flags & FLAGS_SYN) == FLAGS_SYN:
            # A flag SYN estar setada significa que é um cliente tentando estabelecer uma conexão nova
            # TODO: talvez você precise passar mais coisas para o construtor de conexão
            conexao = self.conexoes[id_conexao] = Conexao(self, id_conexao, seq_no)
            # TODO: você precisa fazer o handshake aceitando a conexão. Escolha se você acha melhor
            # fazer aqui mesmo ou dentro da classe Conexao.
            if self.callback:
                self.callback(conexao)
        elif id_conexao in self.conexoes:
            # Passa para a conexão adequada se ela já estiver estabelecida
            self.conexoes[id_conexao]._rdt_rcv(seq_no, ack_no, flags, payload)

            if (flags & FLAGS_FIN) == FLAGS_FIN:
                self.conexoes.pop(id_conexao)
        else:
            print('%s:%d -> %s:%d (pacote associado a conexão desconhecida)' %
                  (src_addr, src_port, dst_addr, dst_port))


class Conexao:
    def __init__(self, servidor, id_conexao, seq_no):
        self.servidor = servidor
        self.id_conexao = id_conexao
        self.callback = None
        self.timer = None
        self.pacotes_sem_confirmacao = []
        self.pacotes_nao_enviados = []
        self.tamanho_pacotes_nao_enviados = 0
        self.tamanho_pacotes_nao_confirmados = 0
        self.tamanho_janela = MSS
        self.estimated_rtt = 0
        self.dev_rtt = 0
        self.timeout_interval = 1

        self.cliente_endereco   = self.id_conexao[0]
        self.cliente_porta      = self.id_conexao[1]
        self.cliente_sequencia  = seq_no

        self.servidor_endereco  = self.id_conexao[2]
        self.servidor_porta     = self.id_conexao[3]
        self.servidor_sequencia = random.randint(0, 0xffff)
        self.servidor_send_base = self.servidor_sequencia

        self.enviar_segmento(nova_conexão=True)

    def _exemplo_timer(self):
        # Esta função é só um exemplo e pode ser removida
        print('Este é um exemplo de como fazer um timer')

    def timeout(self):
        self.tamanho_janela /= 2
        self.enviar_segmento(timeout=True)
        self.iniciar_timer()

    def atualizar_timeout_interval(self, inicio, fim):
        sample_rtt = fim - inicio

        if (self.estimated_rtt == 0):
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.estimated_rtt = (1 - 0.125) * self.estimated_rtt + 0.150 * sample_rtt
            self.dev_rtt = (1 - 0.25) * self.dev_rtt + 0.25 * abs(sample_rtt - self.estimated_rtt)

        self.timeout_interval = self.estimated_rtt + 4 * self.dev_rtt

    def iniciar_timer(self):
        self.parar_timer()
        self.timer = asyncio.get_event_loop().call_later(self.timeout_interval, self.timeout) # um timer pode ser criado assim;

    def parar_timer(self):
        if (self.timer != None):
            self.timer.cancel() # é possível cancelar o timer chamando esse método;
            self.timer = None

    def adicionar_pacote_sem_confirmacao(self, segmento, inicio):
        self.pacotes_sem_confirmacao.append({'inicio' : inicio,
                                            'segmento' : segmento,
                                            'tempo' : time.time(),
                                            'retransmissao': False})
        self.tamanho_pacotes_nao_confirmados += len(segmento)

        if (self.timer == None):
            self.iniciar_timer()

    def atualizar_pacotes_sem_confirmacao(self):
        while (self.pacotes_sem_confirmacao and self.servidor_send_base > self.pacotes_sem_confirmacao[0]['inicio']):
            pacote = self.pacotes_sem_confirmacao.pop(0)
            self.tamanho_pacotes_nao_confirmados -= len(pacote['segmento'])

            if (not pacote['retransmissao']):
                self.atualizar_timeout_interval(pacote['tempo'], time.time())
        
        if (self.pacotes_sem_confirmacao):
            self.iniciar_timer()
        else:
            self.parar_timer()

        self.enviar_pacotes_nao_enviados()

    def criar_cabecalho(self, flags):
        cabecalho = make_header(self.servidor_porta,
                                self.cliente_porta,
                                self.servidor_sequencia,
                                self.cliente_sequencia,
                                flags)
        cabecalho = fix_checksum(cabecalho, self.cliente_endereco, self.servidor_endereco)
        return cabecalho

    def adicionar_pacote_nao_enviado(self, pacote, confirmacao=False, inicio=0):
        self.pacotes_nao_enviados.append({'pacote' : pacote, 'confirmacao' : confirmacao, 'inicio' : inicio})
        self.tamanho_pacotes_nao_enviados += len(pacote)

    def enviar_pacotes_nao_enviados(self):
        while (self.pacotes_nao_enviados and 
                len(self.pacotes_nao_enviados[0]['pacote']) +
                self.tamanho_pacotes_nao_confirmados <= self.tamanho_janela):

            pacote = self.pacotes_nao_enviados.pop(0)
            self.tamanho_pacotes_nao_enviados -= len(pacote['pacote'])
            self.servidor.rede.enviar(pacote['pacote'], self.cliente_endereco)
        
            if (pacote['confirmacao']):
                self.adicionar_pacote_sem_confirmacao(pacote['pacote'], pacote['inicio'])

    def enviar_segmento(self, payload=b'', nova_conexão=False, confirmacao=False,
                        confirmacao_fechamento=False, fechamento_conexao=False, timeout=False):

        if (nova_conexão):
            self.cliente_sequencia += 1 # O primeiro envio conta como 1 byte
            cabecalho = self.criar_cabecalho(FLAGS_SYN | FLAGS_ACK)
            self.servidor_sequencia += 1 # O primeiro envio conta como 1 byte
            self.adicionar_pacote_nao_enviado(cabecalho)
        
        elif (confirmacao):
            self.cliente_sequencia += len(payload)
            cabecalho = self.criar_cabecalho(FLAGS_ACK)
            self.adicionar_pacote_nao_enviado(cabecalho)
        
        elif (confirmacao_fechamento):
            self.cliente_sequencia += 1 # O envio de fechamento conta como 1 byte
            cabecalho = self.criar_cabecalho(FLAGS_ACK)
            self.adicionar_pacote_nao_enviado(cabecalho)
        
        elif (fechamento_conexao):
            cabecalho = self.criar_cabecalho(FLAGS_FIN)
            self.servidor_sequencia += 1 # O envio de fechamento conta como 1 byte
            self.adicionar_pacote_nao_enviado(cabecalho)
        
        elif (timeout):
            segmento = self.pacotes_sem_confirmacao[0]['segmento']
            self.pacotes_sem_confirmacao[0]['retransmissao'] = True
            self.servidor.rede.enviar(segmento, self.cliente_endereco)

        else:
            cabecalho = self.criar_cabecalho(FLAGS_ACK)
            segmento = cabecalho + payload
            self.adicionar_pacote_nao_enviado(segmento, confirmacao=True, inicio=self.servidor_sequencia)
            self.servidor_sequencia += len(payload)

        self.enviar_pacotes_nao_enviados()

    def _rdt_rcv(self, seq_no, ack_no, flags, payload):
        # TODO: trate aqui o recebimento de segmentos provenientes da camada de rede.
        # Chame self.callback(self, dados) para passar dados para a camada de aplicação após
        # garantir que eles não sejam duplicados e que tenham sido recebidos em ordem.
        print('recebido payload: %r' % payload)

        if (ack_no > self.servidor_send_base):
            self.servidor_send_base = ack_no
            self.atualizar_pacotes_sem_confirmacao()
            self.tamanho_janela += MSS
            self.enviar_pacotes_nao_enviados()

        if (flags & FLAGS_FIN) == FLAGS_FIN:
            self.callback(self, b'')
            self.enviar_segmento(confirmacao_fechamento=True)

        elif (len(payload) == 0):
            pass

        elif (self.cliente_sequencia == seq_no):
            self.callback(self, payload)
            self.enviar_segmento(payload, confirmacao=True)

    # Os métodos abaixo fazem parte da API

    def registrar_recebedor(self, callback):
        """
        Usado pela camada de aplicação para registrar uma função para ser chamada
        sempre que dados forem corretamente recebidos
        """
        self.callback = callback

    def enviar(self, dados):
        """
        Usado pela camada de aplicação para enviar dados
        """
        # TODO: implemente aqui o envio de dados.
        # Chame self.servidor.rede.enviar(segmento, dest_addr) para enviar o segmento
        # que você construir para a camada de rede.

        partes = []

        for i in range(0, len(dados), MSS):
            partes.append(dados[i:i+MSS])

        for parte in partes:
            self.enviar_segmento(parte)

    def fechar(self):
        """
        Usado pela camada de aplicação para fechar a conexão
        """
        # TODO: implemente aqui o fechamento de conexão

        self.enviar_segmento(fechamento_conexao=True)